"""Live vehicle movement along a planned route.

Every layer of SEVPS reacts to a vehicle *moving* - the corridor arms ahead of
it, the ETA decays, driver alerts sweep the road in front, the hospital's board
counts down. Until now the only thing that produced that movement was real GPS
telemetry or the ``simulate`` management command in a second terminal. Without
either, a trip was planned, a hospital was assigned, a corridor was armed, and
then the ambulance sat motionless on the map for the whole journey.

This module supplies the missing motion from the route the platform has
already committed to, so a crew watching their own console sees the vehicle
travel from where they are to the hospital they chose.

Two design points worth stating:

**Progress is derived, never stored.** The distance travelled is recovered by
projecting the vehicle's current position onto its active route
(:func:`~apps.brain.eta.compute_progress`) rather than kept in a counter. That
makes every caller idempotent and stateless: the worker, an HTTP tick and a
real GPS fix can interleave in any order without a progress counter drifting
away from where the ambulance actually is.

**It defers to anything already driving the vehicle.** A fix newer than
``FRESH_FIX_S`` means something else - a real device, or ``simulate`` - is in
control, and this module leaves that vehicle alone. Two things writing
positions to one ambulance would make it stutter between two ideas of where
it is.
"""
from __future__ import annotations

import logging
import random

from django.utils import timezone

from apps.core.enums import EmergencyCategory, TripStage, VehicleStatus
from apps.core.geo import Point, bearing_deg, point_along_polyline
from apps.core.realtime import broadcast, broadcast_ops, vehicle_group

log = logging.getLogger("sevps.dispatch.journey")

#: A fix newer than this means something else is driving the vehicle.
FRESH_FIX_S = 3.0

#: Never advance further than this in one step, however long the gap.
#:
#: A console left closed overnight would otherwise resume by teleporting the
#: ambulance the length of its route in a single jump - skipping every corridor
#: arming, driver alert and geofence on the way, all of which are computed from
#: positions as they are reported.
MAX_STEP_S = 20.0

#: Fallback speed where the plan carries no per-step prediction, in km/h.
DEFAULT_SPEED_KMH = 30.0

#: Stages during which an ambulance is actually driving somewhere. ON_SCENE is
#: deliberately absent: it is parked at the patient, and a vehicle that drifts
#: away from the incident while the crew are working is worse than one that
#: does not move at all.
MOVING_STAGES = (TripStage.TO_SCENE, TripStage.TO_HOSPITAL)


def _speed_ms(plan, travelled_m: float) -> float:
    """Speed at this point of the route, taken from the plan's own steps.

    The plan's predicted speeds are what the ETA was computed from, so moving
    at them keeps the marker and the countdown telling the same story.
    """
    walked = 0.0
    for step in plan.steps or []:
        walked += float(step.get("length_m", 0.0))
        if walked >= travelled_m:
            return max(3.0, float(step.get("predicted_speed_kmh", DEFAULT_SPEED_KMH)) / 3.6)
    return DEFAULT_SPEED_KMH / 3.6


def advance_trip(trip, *, now=None) -> dict | None:
    """Move one trip's vehicle along its route. Returns what changed, or None."""
    from apps.brain.eta import compute_progress
    from apps.dispatch import orchestrator

    now = now or timezone.now()
    plan = trip.active_route
    if plan is None or len(plan.geometry or []) < 2:
        return None
    if trip.stage not in MOVING_STAGES:
        return None

    vehicle = trip.vehicle
    last = vehicle.last_seen_at
    elapsed = MAX_STEP_S if last is None else (now - last).total_seconds()
    if elapsed < FRESH_FIX_S:
        return None
    elapsed = min(elapsed, MAX_STEP_S)

    travelled = compute_progress(plan, vehicle.point).distance_along_m
    speed = _speed_ms(plan, travelled)
    travelled = min(plan.total_distance_m, travelled + speed * elapsed)

    points = [Point(lat, lon) for lat, lon in plan.geometry]
    position = point_along_polyline(points, travelled)
    # Heading is taken from a look-ahead point rather than from the last two
    # fixes: at the top of the route, and whenever the vehicle is stationary,
    # consecutive fixes are identical and the bearing between them is
    # undefined - which on a heading-up map spins the whole world.
    ahead = point_along_polyline(points, min(plan.total_distance_m, travelled + 25.0))
    heading = bearing_deg(position.lat, position.lon, ahead.lat, ahead.lon)

    vehicle.record_position(
        position.lat,
        position.lon,
        speed_kmh=round(speed * 3.6, 1),
        heading_deg=heading,
        accuracy_m=8.0,
    )
    # The single call that makes this real rather than cosmetic: corridor
    # arming, ETA decay, driver alerts, replanning and geofenced stage changes
    # all hang off a reported position.
    outcome = orchestrator.on_vehicle_position(vehicle) or {}

    # Publish it the way the telemetry endpoint does. `on_vehicle_position`
    # deliberately does not broadcast - it is the reactive chain, not the
    # transport - so a mover that skipped this would update the database and
    # leave every open map showing the ambulance where it used to be.
    payload = vehicle.as_tracking_payload()
    broadcast_ops("vehicle_position", payload)
    broadcast(vehicle_group(vehicle.callsign), "vehicle_position", payload)

    return {
        "trip_id": trip.id,
        "reference": trip.reference,
        "travelled_m": round(travelled, 1),
        "remaining_m": round(max(0.0, plan.total_distance_m - travelled), 1),
        "stage": outcome.get("stage", trip.stage),
    }


def advance_active_journeys(*, now=None) -> dict:
    """Move every in-transit vehicle one step along its route."""
    from apps.dispatch.models import EmergencyTrip

    now = now or timezone.now()
    trips = (
        EmergencyTrip.objects.active()
        .filter(stage__in=MOVING_STAGES)
        .select_related("vehicle")
        .prefetch_related("routes")
    )

    moved = []
    for trip in trips:
        try:
            result = advance_trip(trip, now=now)
        except Exception:  # pragma: no cover - one bad trip must not stop the rest
            log.warning("journey advance failed for trip %s", trip.id, exc_info=True)
            continue
        if result:
            moved.append(result)

    return {"considered": len(trips), "moved": len(moved), "journeys": moved}


# ---------------------------------------------------------------------------
# Demonstration traffic
# ---------------------------------------------------------------------------
def _random_intersection(rng):
    from apps.network.models import Intersection

    count = Intersection.objects.count()
    if not count:
        return None
    return Intersection.objects.all()[rng.randrange(count)]


#: Case mix for demo responses. Deliberately varied so the map shows a range
#: of priority levels and corridor behaviour rather than nine identical runs.
_DEMO_CATEGORIES = [
    EmergencyCategory.TRAUMA,
    EmergencyCategory.CARDIAC,
    EmergencyCategory.RESPIRATORY,
    EmergencyCategory.STROKE,
    EmergencyCategory.OBSTETRIC,
]


def ensure_demo_traffic(*, rng=None) -> dict:
    """Keep every demonstration unit on a response.

    A demo ambulance with nothing to do is a stationary marker, which is the
    problem these exist to solve. So whenever one finishes - or has never
    started - a fresh synthetic response is opened for it and a hospital
    assigned at once, and it drives the whole way there before starting again.

    Only vehicles explicitly flagged ``is_demo`` are touched. Nothing here can
    reach a real crew's ambulance.
    """
    from apps.dispatch import orchestrator
    from apps.fleet.models import EmergencyVehicle

    rng = rng or random.Random()
    started, running = [], []

    for vehicle in EmergencyVehicle.objects.filter(is_demo=True):
        if vehicle.active_trip is not None:
            running.append(vehicle.callsign)
            continue

        node = _random_intersection(rng)
        if node is None:
            continue

        try:
            trip = orchestrator.create_trip(
                vehicle=vehicle,
                incident_point=Point(node.latitude, node.longitude),
                emergency_category=EmergencyCategory.UNKNOWN,
                incident_address=f"{node.label} (demonstration)",
            )
            # Assigned immediately rather than on scene arrival: a demo unit
            # has no crew to perform an assessment, and without a destination
            # it would stop at the incident and stay there.
            orchestrator.assign_hospital(
                trip, emergency_category=rng.choice(_DEMO_CATEGORIES)
            )
        except Exception:  # pragma: no cover - demo traffic is never load-bearing
            log.warning("could not start demo trip for %s", vehicle.callsign, exc_info=True)
            continue

        started.append(vehicle.callsign)

    return {"started": started, "running": running, "total": len(started) + len(running)}


def _complete_arrived_demo_trips() -> int:
    """Close out demo runs that have reached the hospital.

    Real trips are closed by the crew pressing handover. A demo unit has no
    crew, so without this it would sit at the hospital for ever and the demo
    fleet would wind down to nothing over an afternoon.
    """
    from apps.dispatch import orchestrator
    from apps.dispatch.models import EmergencyTrip

    closed = 0
    arrived = (
        EmergencyTrip.objects.active()
        .filter(stage=TripStage.ARRIVED, vehicle__is_demo=True)
        .select_related("vehicle")
    )
    for trip in arrived:
        orchestrator.advance_stage(trip, TripStage.HANDOVER, reason="demonstration handover")
        trip.vehicle.status = VehicleStatus.AVAILABLE
        trip.vehicle.save(update_fields=["status", "updated_at"])
        closed += 1
    return closed


def tick(*, with_demo: bool = True) -> dict:
    """One movement sweep: close finished demo runs, start new ones, advance all.

    The order matters. Closing first frees a demo unit in the same sweep that
    gives it its next job, so the fleet never shows a gap; advancing last means
    a response opened in this sweep already moves in it, rather than sitting
    still until the next one.
    """
    result: dict = {}
    if with_demo:
        try:
            result["demo_completed"] = _complete_arrived_demo_trips()
            result["demo"] = ensure_demo_traffic()
        except Exception:  # pragma: no cover
            log.warning("demo traffic tick failed", exc_info=True)
            result["demo"] = {"error": True}
    result.update(advance_active_journeys())
    return result
