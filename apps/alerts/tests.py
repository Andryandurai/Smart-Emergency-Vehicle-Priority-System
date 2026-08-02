"""Layer 4 tests: alerts must warn the road ahead, and only the road ahead."""
from django.test import TestCase
from django.utils import timezone

from apps.alerts.dispatcher import broadcast_driver_alerts, build_message, clear_expired_boards
from apps.alerts.models import DisplayBoard, DriverAlert
from apps.brain import graph as graph_mod
from apps.brain.tests import build_grid
from apps.core.enums import PriorityLevel, TripStage, VehicleStatus
from apps.core.geo import Point, haversine_m
from apps.dispatch import orchestrator
from apps.dispatch.siren import apply_priority
from apps.fleet.models import EmergencyVehicle
from apps.hospitals.rules import seed_rules


class MessageTests(TestCase):
    def test_message_matches_the_specification_wording(self):
        message, instruction = build_message(15, PriorityLevel.CRITICAL, "ambulance")
        self.assertIn("Ambulance Approaching", message)
        self.assertIn("move to the left lane", message)
        self.assertIn("15 seconds", message)
        self.assertEqual(instruction, "Please move to the left lane")

    def test_vehicle_type_is_named_correctly(self):
        message, _ = build_message(20, PriorityLevel.CRITICAL, "fire_engine")
        self.assertIn("Fire Engine", message)

    def test_seconds_are_rounded_for_display(self):
        message, _ = build_message(29.6, PriorityLevel.HIGH, "ambulance")
        self.assertIn("30 seconds", message)


class TargetingTests(TestCase):
    def setUp(self):
        seed_rules()
        self.nodes = build_grid(size=6)
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-A1",
            latitude=self.nodes[(0, 0)].latitude, longitude=self.nodes[(0, 0)].longitude,
            status=VehicleStatus.AVAILABLE, last_seen_at=timezone.now(),
        )
        self.trip = orchestrator.create_trip(
            vehicle=self.vehicle,
            incident_point=Point(self.nodes[(5, 5)].latitude, self.nodes[(5, 5)].longitude),
            emergency_category="cardiac",
        )
        apply_priority(self.trip, level=PriorityLevel.CRITICAL, trigger="test", force=True)

    def tearDown(self):
        graph_mod.invalidate()

    def test_alerts_are_issued_for_a_priority_vehicle(self):
        result = broadcast_driver_alerts(self.trip)
        self.assertGreater(result["issued"], 0)
        self.assertTrue(DriverAlert.objects.exists())

    def test_alerts_are_placed_ahead_of_the_vehicle_not_behind(self):
        broadcast_driver_alerts(self.trip)
        plan = self.trip.active_route
        points = [Point(lat, lon) for lat, lon in plan.geometry]

        from apps.core.geo import distance_to_polyline_m

        _, vehicle_along = distance_to_polyline_m(self.vehicle.point, points)
        for alert in DriverAlert.objects.all():
            _, alert_along = distance_to_polyline_m(Point(alert.latitude, alert.longitude), points)
            self.assertGreaterEqual(alert_along, vehicle_along - 5.0)

    def test_level_4_transport_generates_no_alerts(self):
        """Warning drivers about a routine transfer is noise, and noise is ignored."""
        DriverAlert.objects.all().delete()
        apply_priority(
            self.trip, level=PriorityLevel.NON_CRITICAL, trigger="downgrade", force=True
        )
        result = broadcast_driver_alerts(self.trip)
        self.assertEqual(result["issued"], 0)
        self.assertIn("Level 4", result["reason"])
        self.assertFalse(DriverAlert.objects.exists())

    def test_repeat_calls_refresh_rather_than_duplicate(self):
        broadcast_driver_alerts(self.trip)
        first_count = DriverAlert.objects.count()
        second = broadcast_driver_alerts(self.trip)
        self.assertEqual(DriverAlert.objects.count(), first_count)
        self.assertGreater(second["refreshed"], 0)

    def test_alerts_carry_an_eta_and_an_expiry(self):
        broadcast_driver_alerts(self.trip)
        for alert in DriverAlert.objects.all():
            self.assertGreater(alert.eta_seconds, 0)
            self.assertGreater(alert.expires_at, timezone.now())
            self.assertTrue(alert.geohash)

    def test_no_alerts_once_the_trip_is_finished(self):
        self.trip.stage = TripStage.ARRIVED
        self.trip.save(update_fields=["stage"])
        DriverAlert.objects.all().delete()
        result = broadcast_driver_alerts(self.trip)
        self.assertEqual(result["issued"], 0)


class DisplayBoardTests(TestCase):
    def setUp(self):
        seed_rules()
        self.nodes = build_grid(size=6)
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-B1",
            latitude=self.nodes[(0, 0)].latitude, longitude=self.nodes[(0, 0)].longitude,
            status=VehicleStatus.AVAILABLE, last_seen_at=timezone.now(),
        )
        self.trip = orchestrator.create_trip(
            vehicle=self.vehicle,
            incident_point=Point(self.nodes[(5, 5)].latitude, self.nodes[(5, 5)].longitude),
            emergency_category="cardiac",
        )
        apply_priority(self.trip, level=PriorityLevel.CRITICAL, trigger="test", force=True)

    def tearDown(self):
        graph_mod.invalidate()

    def test_a_board_on_the_route_lights_up(self):
        plan = self.trip.active_route
        # Place a board on the route, a little way ahead of the vehicle.
        from apps.core.geo import point_along_polyline

        spot = point_along_polyline(
            [Point(lat, lon) for lat, lon in plan.geometry],
            min(400.0, plan.total_distance_m * 0.3),
        )
        board = DisplayBoard.objects.create(
            code="VMS-T1", name="Test board", latitude=spot.lat, longitude=spot.lon
        )
        broadcast_driver_alerts(self.trip)
        board.refresh_from_db()
        self.assertTrue(board.current_message)
        self.assertTrue(board.is_displaying_alert)

    def test_a_distant_board_stays_blank(self):
        board = DisplayBoard.objects.create(
            code="VMS-T2", name="Far board", latitude=12.5, longitude=79.0
        )
        broadcast_driver_alerts(self.trip)
        board.refresh_from_db()
        self.assertEqual(board.current_message, "")

    def test_expired_messages_are_cleared(self):
        board = DisplayBoard.objects.create(
            code="VMS-T3", name="Stale board", latitude=13.0, longitude=80.0,
            current_message="Old message",
            message_expires_at=timezone.now() - timezone.timedelta(minutes=5),
        )
        self.assertEqual(clear_expired_boards(), 1)
        board.refresh_from_db()
        self.assertEqual(board.current_message, "")
