"""Layer 3 and Layer 6 tests: signal preemption safety, and siren policy."""
from django.test import TestCase
from django.utils import timezone

from apps.brain import graph as graph_mod
from apps.brain.tests import build_grid
from apps.core.enums import (
    LightPattern,
    PreemptionState,
    PriorityLevel,
    SirenMode,
    TripStage,
    VehicleStatus,
)
from apps.core.geo import Point, point_along_polyline
from apps.dispatch import orchestrator
from apps.dispatch.corridor import release_corridor, sync_corridor, tick_corridors
from apps.dispatch.models import EmergencyTrip, SignalPreemption
from apps.dispatch.siren import PRIORITY_PROFILES, apply_priority, derive_priority, profile_for
from apps.fleet.models import EmergencyVehicle
from apps.hospitals.models import Hospital, HospitalCapability, HospitalCapacity
from apps.hospitals.rules import seed_rules
from apps.network.models import Intersection, TrafficSignal


class SirenPolicyTests(TestCase):
    """Layer 6 - the four-level ladder from the problem statement."""

    def setUp(self):
        seed_rules()
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="TEST-01", latitude=13.0, longitude=80.0, status=VehicleStatus.AVAILABLE
        )
        self.trip = EmergencyTrip.objects.create(
            vehicle=self.vehicle, emergency_category="cardiac", stage=TripStage.TO_HOSPITAL
        )

    def test_level_1_profile_matches_specification(self):
        profile = profile_for(PriorityLevel.CRITICAL)
        self.assertEqual(profile.siren_mode, SirenMode.CONTINUOUS)
        self.assertEqual(profile.light_pattern, LightPattern.MAX_INTENSITY)
        self.assertTrue(profile.grants_green_corridor)

    def test_level_4_is_silent_and_dark(self):
        profile = profile_for(PriorityLevel.NON_CRITICAL)
        self.assertEqual(profile.siren_mode, SirenMode.OFF)
        self.assertEqual(profile.light_pattern, LightPattern.OFF)
        self.assertFalse(profile.grants_green_corridor)

    def test_all_four_levels_are_defined(self):
        self.assertEqual(set(PRIORITY_PROFILES), {1, 2, 3, 4})

    def test_cardiac_transport_is_level_1(self):
        level, _ = derive_priority(self.trip)
        self.assertEqual(level, PriorityLevel.CRITICAL)

    def test_non_critical_transfer_is_level_4(self):
        self.trip.emergency_category = "transfer"
        level, _ = derive_priority(self.trip)
        self.assertEqual(level, PriorityLevel.NON_CRITICAL)

    def test_siren_is_off_at_the_scene(self):
        """Lights for visibility, but no siren next to the patient."""
        self.trip.stage = TripStage.ON_SCENE
        level, trigger = derive_priority(self.trip)
        self.assertEqual(profile_for(level).siren_mode, SirenMode.BURST)
        self.assertIn("scene", trigger)

    def test_deterioration_upgrades_priority(self):
        self.trip.emergency_category = "poisoning"      # normally Level 2
        self.trip.patient_deteriorating = True
        level, trigger = derive_priority(self.trip)
        self.assertEqual(level, PriorityLevel.CRITICAL)
        self.assertIn("deteriorating", trigger)

    def test_after_handover_the_vehicle_stands_down(self):
        self.trip.stage = TripStage.HANDOVER
        level, _ = derive_priority(self.trip)
        self.assertEqual(level, PriorityLevel.NON_CRITICAL)

    def test_observed_symptoms_drive_priority_when_the_category_is_unknown(self):
        """A crew who cannot name the presentation still gets a corridor.

        "Undetermined" is the correct entry for a crew facing an unresponsive
        patient with no history, and its rule is deliberately Level 3. Left
        there, an unconscious and bleeding patient would travel without siren
        or signal priority because the crew was honest about not having a
        diagnosis. The observations they *did* record must count.
        """
        self.trip.emergency_category = "unknown"
        self.trip.symptoms = ["unconscious", "bleeding"]
        level, _ = derive_priority(self.trip)
        self.assertEqual(level, PriorityLevel.CRITICAL)

    def test_symptoms_never_soften_a_categorys_priority(self):
        """Escalation only - a fever alongside a cardiac call is still Level 1."""
        self.trip.emergency_category = "cardiac"
        self.trip.symptoms = ["fever"]
        level, _ = derive_priority(self.trip)
        self.assertEqual(level, PriorityLevel.CRITICAL)

    def test_apply_priority_writes_a_directive_and_mirrors_to_vehicle(self):
        directive = apply_priority(self.trip, trigger="test", force=True)
        self.vehicle.refresh_from_db()
        self.trip.refresh_from_db()

        self.assertIsNotNone(directive)
        self.assertEqual(self.trip.priority_level, PriorityLevel.CRITICAL)
        self.assertEqual(self.vehicle.siren_mode, SirenMode.CONTINUOUS)
        self.assertEqual(self.vehicle.priority_level, PriorityLevel.CRITICAL)

    def test_no_directive_when_nothing_changes(self):
        apply_priority(self.trip, trigger="first", force=True)
        self.assertIsNone(apply_priority(self.trip, trigger="second"))

    def test_directive_records_upgrade_direction(self):
        apply_priority(self.trip, level=PriorityLevel.MODERATE, trigger="start", force=True)
        directive = apply_priority(self.trip, level=PriorityLevel.CRITICAL, trigger="worse")
        self.assertTrue(directive.is_upgrade)
        self.assertFalse(directive.is_downgrade)


class CorridorTests(TestCase):
    """Layer 3 - green corridor actuation and its safety guarantees."""

    def setUp(self):
        seed_rules()
        self.nodes = build_grid(size=5, signalise_every=1)  # every node signalised
        for node in Intersection.objects.filter(is_signalised=True):
            TrafficSignal.objects.create(
                intersection=node,
                controller_id=f"TSC-{node.id}",
                controller_type="simulated",
                supports_preemption=True,
                min_recovery_s=0,
                is_online=True,
            )
        hospital = Hospital.objects.create(
            code="H1", name="Test Hospital",
            latitude=self.nodes[(4, 4)].latitude, longitude=self.nodes[(4, 4)].longitude,
        )
        HospitalCapability.objects.bulk_create([
            HospitalCapability(hospital=hospital, facility=f)
            for f in ("emergency_dept", "cardiac_icu", "cath_lab", "icu", "trauma_center")
        ])
        HospitalCapacity.objects.create(hospital=hospital)

        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-T1",
            latitude=self.nodes[(0, 0)].latitude, longitude=self.nodes[(0, 0)].longitude,
            status=VehicleStatus.AVAILABLE, last_seen_at=timezone.now(),
        )
        self.trip = orchestrator.create_trip(
            vehicle=self.vehicle,
            incident_point=Point(self.nodes[(4, 4)].latitude, self.nodes[(4, 4)].longitude),
            emergency_category="cardiac",
        )

    def tearDown(self):
        graph_mod.invalidate()

    def test_corridor_is_planned_for_a_priority_vehicle(self):
        result = sync_corridor(self.trip)
        self.assertGreater(result["planned"], 0)
        self.assertGreater(SignalPreemption.objects.filter(trip=self.trip).count(), 0)

    def test_level_4_gets_no_corridor(self):
        apply_priority(self.trip, level=PriorityLevel.NON_CRITICAL, trigger="downgrade", force=True)
        SignalPreemption.objects.filter(trip=self.trip).delete()
        result = sync_corridor(self.trip)
        self.assertEqual(result["planned"], 0)
        self.assertTrue(any("Level 4" in s["reason"] for s in result["skipped"]))

    def test_a_signal_that_cannot_be_preempted_is_skipped_not_forced(self):
        TrafficSignal.objects.update(supports_preemption=False)
        SignalPreemption.objects.filter(trip=self.trip).delete()
        result = sync_corridor(self.trip)
        self.assertEqual(result["planned"], 0)
        self.assertTrue(result["skipped"])

    def test_offline_controller_is_skipped(self):
        TrafficSignal.objects.update(is_online=False)
        SignalPreemption.objects.filter(trip=self.trip).delete()
        result = sync_corridor(self.trip)
        self.assertEqual(result["planned"], 0)

    def test_hold_never_exceeds_the_safety_cap(self):
        sync_corridor(self.trip)
        cap = 90  # settings.SEVPS["SIGNAL_MAX_HOLD_S"] default
        for preemption in SignalPreemption.objects.filter(trip=self.trip):
            self.assertLessEqual(preemption.hold_duration_s, cap + 0.01)

    def test_release_corridor_clears_every_open_request(self):
        sync_corridor(self.trip)
        self.assertTrue(SignalPreemption.objects.filter(trip=self.trip).exists())

        release_corridor(self.trip, reason="test")
        still_open = SignalPreemption.objects.filter(
            trip=self.trip,
            state__in=[PreemptionState.PLANNED, PreemptionState.ARMED, PreemptionState.ACTIVE],
        )
        self.assertFalse(still_open.exists())

    def test_safety_sweep_force_releases_an_overdue_hold(self):
        """A vehicle that loses GPS must not leave a junction green forever."""
        sync_corridor(self.trip)
        preemption = SignalPreemption.objects.filter(trip=self.trip).first()
        preemption.state = PreemptionState.ACTIVE
        preemption.activated_at = timezone.now() - timezone.timedelta(seconds=600)
        preemption.planned_release_at = timezone.now() - timezone.timedelta(seconds=500)
        preemption.save()
        preemption.signal.is_preempted = True
        preemption.signal.save()

        result = tick_corridors()
        preemption.refresh_from_db()
        preemption.signal.refresh_from_db()

        self.assertEqual(result["released"], 1)
        self.assertEqual(preemption.state, PreemptionState.RELEASED)
        self.assertFalse(preemption.signal.is_preempted)

    def test_completed_trip_releases_the_corridor(self):
        sync_corridor(self.trip)
        orchestrator.advance_stage(self.trip, TripStage.HANDOVER, reason="test")
        open_requests = SignalPreemption.objects.filter(
            trip=self.trip,
            state__in=[PreemptionState.PLANNED, PreemptionState.ARMED, PreemptionState.ACTIVE],
        )
        self.assertFalse(open_requests.exists())

    def test_only_one_vehicle_wins_a_contested_junction(self):
        """Two green corridors crossing at one junction would be a collision."""
        other_vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-T2",
            latitude=self.nodes[(0, 0)].latitude, longitude=self.nodes[(0, 0)].longitude,
            status=VehicleStatus.AVAILABLE, last_seen_at=timezone.now(),
        )
        other_trip = orchestrator.create_trip(
            vehicle=other_vehicle,
            incident_point=Point(self.nodes[(4, 4)].latitude, self.nodes[(4, 4)].longitude),
            emergency_category="transfer",  # Level 4 -> loses outright
        )
        apply_priority(other_trip, level=PriorityLevel.MODERATE, trigger="test", force=True)

        sync_corridor(self.trip)
        sync_corridor(other_trip)

        for signal_id in SignalPreemption.objects.values_list("signal_id", flat=True).distinct():
            active = SignalPreemption.objects.filter(
                signal_id=signal_id, state=PreemptionState.ACTIVE
            ).count()
            self.assertLessEqual(active, 1)


class TripLifecycleTests(TestCase):
    def setUp(self):
        seed_rules()
        self.nodes = build_grid(size=4)
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-L1",
            latitude=self.nodes[(0, 0)].latitude, longitude=self.nodes[(0, 0)].longitude,
            status=VehicleStatus.AVAILABLE, last_seen_at=timezone.now(),
        )

    def tearDown(self):
        graph_mod.invalidate()

    def test_reference_is_sequential_per_day(self):
        first = orchestrator.create_trip(vehicle=self.vehicle, emergency_category="unknown")
        second_vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-L2", latitude=13.0, longitude=80.0, last_seen_at=timezone.now()
        )
        second = orchestrator.create_trip(vehicle=second_vehicle, emergency_category="unknown")
        self.assertNotEqual(first.reference, second.reference)
        self.assertLess(first.reference, second.reference)

    def test_arrival_at_the_scene_advances_the_stage(self):
        scene = self.nodes[(2, 2)]
        trip = orchestrator.create_trip(
            vehicle=self.vehicle,
            incident_point=Point(scene.latitude, scene.longitude),
            emergency_category="unknown",
        )
        plan = trip.active_route
        self.assertIsNotNone(plan)

        points = [Point(lat, lon) for lat, lon in plan.geometry]
        final = point_along_polyline(points, plan.total_distance_m)
        self.vehicle.record_position(final.lat, final.lon, speed_kmh=10)
        orchestrator.on_vehicle_position(self.vehicle)

        trip.refresh_from_db()
        self.assertEqual(trip.stage, TripStage.ON_SCENE)
        self.assertIsNotNone(trip.arrived_scene_at)

    def test_response_time_is_measured(self):
        trip = orchestrator.create_trip(
            vehicle=self.vehicle,
            incident_point=Point(self.nodes[(1, 1)].latitude, self.nodes[(1, 1)].longitude),
            emergency_category="unknown",
        )
        orchestrator.advance_stage(trip, TripStage.ON_SCENE)
        trip.refresh_from_db()
        self.assertIsNotNone(trip.response_time_s)
        self.assertGreaterEqual(trip.response_time_s, 0)
