"""Real-time layer tests.

The WebSocket surface is how every dashboard and field device actually
receives state, so it is exercised through the real ASGI application and the
real channel layer rather than by calling consumer methods directly.
"""
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase
from django.utils import timezone

from apps.core.realtime import broadcast_ops, driver_group, broadcast, hospital_group
from apps.fleet.models import EmergencyVehicle
from apps.hospitals.models import Hospital, HospitalCapability, HospitalCapacity
from sevps.asgi import application


async def connect(path):
    communicator = WebsocketCommunicator(application, path)
    connected, _ = await communicator.connect()
    return communicator, connected


class OpsSocketTests(TransactionTestCase):
    async def test_connect_receives_a_snapshot(self):
        """A control room opening mid-incident must not wait for the next event."""
        communicator, connected = await connect("/ws/ops/")
        self.assertTrue(connected)

        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "snapshot")
        for key in ("vehicles", "trips", "signals", "hospitals", "events"):
            self.assertIn(key, message["data"])

        await communicator.disconnect()

    async def test_broadcast_reaches_a_subscriber(self):
        communicator, _ = await connect("/ws/ops/")
        await communicator.receive_json_from(timeout=5)  # snapshot

        await database_sync_to_async(broadcast_ops)("test_event", {"hello": "world"})
        message = await communicator.receive_json_from(timeout=5)

        self.assertEqual(message["event"], "test_event")
        self.assertEqual(message["data"]["hello"], "world")
        await communicator.disconnect()

    async def test_ping_is_answered(self):
        """Application-level keepalive - idle sockets get dropped by proxies."""
        communicator, _ = await connect("/ws/ops/")
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({"type": "ping"})
        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "pong")
        await communicator.disconnect()

    async def test_malformed_payload_is_rejected_not_fatal(self):
        communicator, _ = await connect("/ws/ops/")
        await communicator.receive_json_from(timeout=5)

        await communicator.send_to(text_data="{not json")
        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "error")
        await communicator.disconnect()


class VehicleSocketTests(TransactionTestCase):
    def setUp(self):
        self.vehicle = EmergencyVehicle.objects.create(
            callsign="AMB-WS1", latitude=13.0, longitude=80.0, last_seen_at=timezone.now()
        )

    async def test_known_vehicle_connects_and_gets_state(self):
        communicator, connected = await connect("/ws/vehicle/AMB-WS1/")
        self.assertTrue(connected)

        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "snapshot")
        self.assertEqual(message["data"]["vehicle"]["callsign"], "AMB-WS1")
        self.assertIn("priority_profile", message["data"])
        await communicator.disconnect()

    async def test_unknown_vehicle_is_refused(self):
        communicator = WebsocketCommunicator(application, "/ws/vehicle/NOT-A-VEHICLE/")
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_telemetry_over_the_socket_is_accepted(self):
        """A vehicle in poor coverage should not pay an HTTP handshake per fix."""
        communicator, _ = await connect("/ws/vehicle/AMB-WS1/")
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to(
            {"type": "telemetry", "latitude": 13.01, "longitude": 80.01, "speed_kmh": 40}
        )
        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "telemetry_ack")

        vehicle = await database_sync_to_async(EmergencyVehicle.objects.get)(callsign="AMB-WS1")
        self.assertAlmostEqual(vehicle.latitude, 13.01, places=5)
        await communicator.disconnect()

    async def test_bad_telemetry_returns_an_error_not_a_crash(self):
        communicator, _ = await connect("/ws/vehicle/AMB-WS1/")
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({"type": "telemetry", "latitude": "not-a-number"})
        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "error")
        await communicator.disconnect()


class HospitalSocketTests(TransactionTestCase):
    def setUp(self):
        self.hospital = Hospital.objects.create(
            code="WSH", name="WS Test Hospital", latitude=13.05, longitude=80.25
        )
        HospitalCapability.objects.create(hospital=self.hospital, facility="emergency_dept")
        HospitalCapacity.objects.create(hospital=self.hospital)

    async def test_hospital_receives_its_own_feed(self):
        communicator, connected = await connect("/ws/hospital/WSH/")
        self.assertTrue(connected)

        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "snapshot")
        self.assertEqual(message["data"]["hospital"]["code"], "WSH")
        self.assertIn("inbound", message["data"])
        self.assertIn("capacity", message["data"])
        await communicator.disconnect()

    async def test_unknown_hospital_is_refused(self):
        communicator = WebsocketCommunicator(application, "/ws/hospital/NOPE/")
        connected, _ = await communicator.connect()
        self.assertFalse(connected)
        await communicator.disconnect()

    async def test_hospital_only_receives_its_own_events(self):
        communicator, _ = await connect("/ws/hospital/WSH/")
        await communicator.receive_json_from(timeout=5)

        await database_sync_to_async(broadcast)(
            hospital_group("OTHER"), "inbound_update", {"trip_id": 99}
        )
        await database_sync_to_async(broadcast)(
            hospital_group("WSH"), "inbound_update", {"trip_id": 1}
        )

        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["data"]["trip_id"], 1)
        await communicator.disconnect()


class DriverSocketTests(TransactionTestCase):
    async def test_position_subscribes_to_surrounding_cells(self):
        """Subscribing to neighbouring cells stops boundary drivers being missed."""
        communicator, connected = await connect("/ws/drivers/")
        self.assertTrue(connected)
        await communicator.receive_json_from(timeout=5)  # empty snapshot

        await communicator.send_json_to(
            {"type": "position", "device_id": "d1", "latitude": 13.06, "longitude": 80.25}
        )
        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "subscribed")
        self.assertGreaterEqual(len(message["data"]["cells"]), 4)
        await communicator.disconnect()

    async def test_alert_in_my_cell_reaches_me(self):
        communicator, _ = await connect("/ws/drivers/?lat=13.06&lon=80.25")
        await communicator.receive_json_from(timeout=5)  # snapshot

        await database_sync_to_async(broadcast)(
            driver_group(13.06, 80.25),
            "driver_alert",
            {"message": "Ambulance Approaching", "eta_seconds": 15},
        )
        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "driver_alert")
        self.assertIn("Ambulance", message["data"]["message"])
        await communicator.disconnect()

    async def test_alert_far_away_does_not_reach_me(self):
        communicator, _ = await connect("/ws/drivers/?lat=13.06&lon=80.25")
        await communicator.receive_json_from(timeout=5)

        # Bengaluru - a different geohash cell entirely.
        await database_sync_to_async(broadcast)(
            driver_group(12.97, 77.59), "driver_alert", {"message": "Not for you"}
        )
        self.assertTrue(await communicator.receive_nothing(timeout=1))
        await communicator.disconnect()

    async def test_missing_position_is_reported(self):
        communicator, _ = await connect("/ws/drivers/")
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({"type": "position", "device_id": "d2"})
        message = await communicator.receive_json_from(timeout=5)
        self.assertEqual(message["event"], "error")
        await communicator.disconnect()
