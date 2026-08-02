"""Drive the whole platform end to end without any real vehicles.

Creates emergencies, moves ambulances along the routes SEVPS computes for
them, performs the on-scene assessment, and lets every layer react exactly as
it would in production - the same orchestrator, the same corridor logic, the
same WebSocket events.  Open the dashboards while this runs.

    python manage.py simulate --trips 3
    python manage.py simulate --trips 5 --speed 4 --events --duration 600
"""
from __future__ import annotations

import random
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.enums import EmergencyCategory, RoadEventType, TripStage, VehicleStatus
from apps.core.geo import Point, bearing_deg, point_along_polyline
from apps.dispatch import orchestrator
from apps.dispatch.corridor import tick_corridors
from apps.dispatch.models import EmergencyTrip
from apps.fleet.models import EmergencyVehicle
from apps.network.models import Intersection, RoadEvent, RoadSegment

# Realistic case mix for an Indian metro EMS service.
CASE_MIX = [
    (EmergencyCategory.TRAUMA, 0.26),
    (EmergencyCategory.CARDIAC, 0.19),
    (EmergencyCategory.RESPIRATORY, 0.13),
    (EmergencyCategory.STROKE, 0.11),
    (EmergencyCategory.OBSTETRIC, 0.08),
    (EmergencyCategory.POISONING, 0.07),
    (EmergencyCategory.PEDIATRIC, 0.06),
    (EmergencyCategory.BURN, 0.05),
    (EmergencyCategory.TRANSFER, 0.05),
]


class Command(BaseCommand):
    help = "Run a live end-to-end SEVPS simulation."

    def add_arguments(self, parser):
        parser.add_argument("--trips", type=int, default=3, help="Concurrent emergencies.")
        parser.add_argument("--tick", type=float, default=2.0, help="Seconds between GPS fixes.")
        parser.add_argument(
            "--speed", type=float, default=3.0,
            help="Simulation speed multiplier (1.0 = real time).",
        )
        parser.add_argument("--duration", type=int, default=0, help="Stop after N seconds (0 = run forever).")
        parser.add_argument("--events", action="store_true", help="Inject random road events.")
        parser.add_argument("--seed", type=int, default=None)

    def handle(self, *args, **options):
        rng = random.Random(options["seed"])
        #: trip id -> the category the crew will confirm once on scene.
        self._pending_category: dict[int, str] = {}
        if not Intersection.objects.exists():
            self.stderr.write(self.style.ERROR("No road network. Run: python manage.py seed_demo"))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Simulating {options['trips']} concurrent emergencies "
            f"at {options['speed']}x. Ctrl-C to stop.\n"
        ))

        started = time.monotonic()
        # Per-vehicle simulated distance travelled along the active route.
        progress_m: dict[int, float] = {}

        try:
            while True:
                if options["duration"] and time.monotonic() - started > options["duration"]:
                    break

                self._ensure_trips(rng, options["trips"])
                self._advance_all(rng, progress_m, options["tick"] * options["speed"])
                tick_corridors()

                if options["events"] and rng.random() < 0.04:
                    self._inject_event(rng)

                time.sleep(options["tick"])
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nSimulation stopped."))

    # ------------------------------------------------------------------ trips
    def _ensure_trips(self, rng, target: int) -> None:
        active = EmergencyTrip.objects.active().count()
        if active >= target:
            return

        vehicle = EmergencyVehicle.objects.filter(
            status=VehicleStatus.AVAILABLE, vehicle_type="ambulance"
        ).first()
        if vehicle is None:
            # Recycle a returning unit so a long run does not exhaust the fleet.
            vehicle = EmergencyVehicle.objects.filter(status=VehicleStatus.RETURNING).first()
            if vehicle is None:
                return
            vehicle.status = VehicleStatus.AVAILABLE
            vehicle.save(update_fields=["status", "updated_at"])

        node = self._random_node(rng)
        category = rng.choices([c for c, _ in CASE_MIX], weights=[w for _, w in CASE_MIX])[0]

        trip = orchestrator.create_trip(
            vehicle=vehicle,
            incident_point=Point(node.latitude, node.longitude),
            emergency_category=EmergencyCategory.UNKNOWN,  # unknown until assessed
            incident_address=node.label,
            caller_number=f"+9198{rng.randint(10000000, 99999999)}",
        )
        # Remember the dispatcher's suspicion for the on-scene assessment.
        trip.patient_notes = f"Caller reports: suspected {category.replace('_', ' ')}"
        trip.save(update_fields=["patient_notes", "updated_at"])
        self._pending_category[trip.id] = category

        self.stdout.write(
            f"  [{timezone.localtime():%H:%M:%S}] {trip.reference}: {vehicle.callsign} "
            f"dispatched to {node.label}"
        )

    def _random_node(self, rng) -> Intersection:
        count = Intersection.objects.count()
        return Intersection.objects.all()[rng.randrange(count)]

    # --------------------------------------------------------------- movement
    def _advance_all(self, rng, progress_m: dict, distance_seconds: float) -> None:
        trips = list(
            EmergencyTrip.objects.active().select_related("vehicle").prefetch_related("routes")
        )
        for trip in trips:
            plan = trip.active_route
            if plan is None or len(plan.geometry or []) < 2:
                continue

            travelled = progress_m.get(trip.vehicle_id, 0.0)
            speed_ms = self._speed_for(plan, travelled)
            travelled = min(plan.total_distance_m, travelled + speed_ms * distance_seconds)
            progress_m[trip.vehicle_id] = travelled

            points = [Point(lat, lon) for lat, lon in plan.geometry]
            position = point_along_polyline(points, travelled)
            ahead = point_along_polyline(points, min(plan.total_distance_m, travelled + 25))
            heading = bearing_deg(position.lat, position.lon, ahead.lat, ahead.lon)

            vehicle = trip.vehicle
            vehicle.record_position(
                position.lat, position.lon,
                speed_kmh=speed_ms * 3.6,
                heading_deg=heading,
                accuracy_m=6.0,
            )
            outcome = orchestrator.on_vehicle_position(vehicle)

            stage_changed = (outcome or {}).get("stage_change")
            if stage_changed:
                progress_m[trip.vehicle_id] = 0.0

            trip.refresh_from_db()
            self._handle_stage(rng, trip, progress_m)

    def _speed_for(self, plan, travelled_m: float) -> float:
        """Speed in m/s at this point of the route, from the plan's own steps."""
        walked = 0.0
        for step in plan.steps or []:
            walked += float(step.get("length_m", 0.0))
            if walked >= travelled_m:
                return max(3.0, float(step.get("predicted_speed_kmh", 30.0)) / 3.6)
        return 30.0 / 3.6

    def _handle_stage(self, rng, trip, progress_m: dict) -> None:
        """Perform the crew actions the simulation is standing in for."""
        if trip.stage == TripStage.ON_SCENE and trip.destination_hospital_id is None:
            category = self._pending_category.pop(trip.id, None) or rng.choices(
                [c for c, _ in CASE_MIX], weights=[w for _, w in CASE_MIX]
            )[0]
            trip.patient_age = rng.randint(3, 88)
            trip.patient_deteriorating = rng.random() < 0.12
            trip.save(update_fields=["patient_age", "patient_deteriorating", "updated_at"])

            result = orchestrator.assign_hospital(trip, emergency_category=category)
            progress_m[trip.vehicle_id] = 0.0

            hospital = result.get("trip", {}).get("hospital", {})
            self.stdout.write(
                f"  [{timezone.localtime():%H:%M:%S}] {trip.reference}: assessed as "
                f"{category} -> {hospital.get('name', 'no hospital')} "
                f"(L{trip.priority_level}, siren {trip.siren_mode})"
            )

        elif trip.stage == TripStage.ARRIVED:
            orchestrator.advance_stage(trip, TripStage.HANDOVER, reason="simulated handover")
            trip.vehicle.status = VehicleStatus.AVAILABLE
            trip.vehicle.save(update_fields=["status", "updated_at"])
            progress_m.pop(trip.vehicle_id, None)
            response = trip.response_time_s
            transport = trip.transport_time_s
            self.stdout.write(self.style.SUCCESS(
                f"  [{timezone.localtime():%H:%M:%S}] {trip.reference}: handover complete "
                f"(response {response and round(response)}s, transport {transport and round(transport)}s)"
            ))

    # ----------------------------------------------------------------- events
    def _inject_event(self, rng) -> None:
        """Drop a disruption on the network to exercise dynamic replanning."""
        count = RoadSegment.objects.count()
        if not count:
            return
        segment = RoadSegment.objects.all()[rng.randrange(count)]
        event_type, severity = rng.choice(
            [
                (RoadEventType.ACCIDENT, 0.8),
                (RoadEventType.CONGESTION, 0.55),
                (RoadEventType.WATERLOGGING, 0.65),
                (RoadEventType.PUBLIC_EVENT, 0.45),
                (RoadEventType.CLOSURE, 0.98),
            ]
        )
        event = RoadEvent.objects.create(
            event_type=event_type,
            segment=segment,
            latitude=segment.latitude,
            longitude=segment.longitude,
            severity=severity,
            confidence=round(rng.uniform(0.6, 1.0), 2),
            description=f"Simulated {event_type} on {segment.name}",
            source="simulation",
            ends_at=timezone.now() + timezone.timedelta(minutes=rng.randint(5, 25)),
        )
        self.stdout.write(self.style.WARNING(
            f"  [{timezone.localtime():%H:%M:%S}] disruption: {event_type} on {segment.name}"
        ))

        from apps.brain.rerouting import reassess_active_trips

        for outcome in reassess_active_trips(reason=f"{event_type} reported"):
            if outcome.get("should_reroute"):
                self.stdout.write(f"      trip {outcome['trip_id']} rerouted: {outcome['reason']}")
