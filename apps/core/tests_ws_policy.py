"""Phase 5 tests: WebSocket authorisation, redaction, coalescing, live pushes.

The regressions guarded here were live defects found by probing the running
server, not hypotheticals:

* ``/ws/hospital/<code>/`` served patient age and clinical notes to an
  anonymous socket, because the snapshot built its payload from a model method
  that predated the Phase 2 redaction and therefore bypassed it.
* ``/ws/signals/`` accepted ``phase_report`` - which writes traffic-controller
  state - from an anonymous client.
"""
from __future__ import annotations

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import Group, User
from django.test import SimpleTestCase, TransactionTestCase

from apps.core.roles import CLINICAL_ROLES, Role
from apps.core.ws_policy import POLICIES
from sevps.asgi import application


@database_sync_to_async
def make_user(username: str, *roles: str, superuser: bool = False) -> User:
    user = User.objects.create_user(username, password="pw", is_superuser=superuser)
    for role in roles:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


@database_sync_to_async
def token_for(user) -> str:
    from apps.core.jwt import SEVPSTokenObtainPairSerializer

    return str(SEVPSTokenObtainPairSerializer.get_token(user).access_token)


async def connect(path: str, token: str | None = None):
    url = f"{path}{'?' if '?' not in path else '&'}token={token}" if token else path
    communicator = WebsocketCommunicator(application, url)
    connected, code = await communicator.connect()
    return communicator, connected, code


# ---------------------------------------------------------------------------
# Policy declarations
# ---------------------------------------------------------------------------
class PolicyAuditTests(SimpleTestCase):
    def test_every_registered_consumer_declares_a_policy(self):
        """A consumer without a policy fails closed - assert none exists."""
        from sevps.routing import websocket_urlpatterns

        undeclared = []
        for route in websocket_urlpatterns:
            consumer = route.callback
            cls = getattr(consumer, "consumer_class", None)
            if cls is None or getattr(cls, "policy", None) is None:
                undeclared.append(str(route.pattern))
        self.assertEqual(undeclared, [], f"consumers without a declared policy: {undeclared}")

    def test_clinical_sockets_are_never_anonymous(self):
        """The exact defect found on /ws/hospital/."""
        offenders = [
            policy.name
            for policy in POLICIES.values()
            if policy.carries_clinical_data and policy.allow_anonymous
        ]
        self.assertEqual(offenders, [], f"anonymous sockets carrying patient data: {offenders}")

    def test_command_accepting_sockets_are_authorised(self):
        """A read-only leak is bad; an unauthenticated write is worse.

        The drivers socket is the deliberate exception: it accepts a position
        report used only to pick a geohash cell for alert targeting, and is
        never linked to an identity.
        """
        offenders = [
            policy.name
            for policy in POLICIES.values()
            if policy.accepts_commands and policy.allow_anonymous and policy.name != "drivers"
        ]
        self.assertEqual(offenders, [], f"anonymous command sockets: {offenders}")

    def test_hospital_socket_is_restricted_to_clinical_roles(self):
        """Its payload is unredacted for group broadcasts, so entry must gate."""
        allowed = set(POLICIES["hospital"].required_roles)
        self.assertTrue(
            allowed <= set(CLINICAL_ROLES),
            f"non-clinical roles may open the hospital socket: {allowed - set(CLINICAL_ROLES)}",
        )

    def test_every_public_socket_records_why(self):
        for policy in POLICIES.values():
            if policy.allow_anonymous:
                self.assertTrue(policy.reason, f"{policy.name} is public with no recorded reason")


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------
class SocketAuthorisationTests(TransactionTestCase):
    async def test_anonymous_cannot_open_the_hospital_socket(self):
        """Regression: this leaked patient age and clinical notes."""
        from apps.hospitals.models import Hospital, HospitalCapacity

        @database_sync_to_async
        def seed():
            hospital = Hospital.objects.create(
                code="WSP", name="WS Policy Hospital", latitude=13.0, longitude=80.0
            )
            HospitalCapacity.objects.create(hospital=hospital)

        await seed()
        communicator, connected, code = await connect("/ws/hospital/WSP/")
        self.assertFalse(connected)
        self.assertEqual(code, 4401)
        await communicator.disconnect()

    async def test_traffic_police_cannot_open_the_hospital_socket(self):
        """Data minimisation: a corridor needs no diagnosis."""
        from apps.hospitals.models import Hospital, HospitalCapacity

        @database_sync_to_async
        def seed():
            hospital = Hospital.objects.create(
                code="WSP2", name="WS Policy Hospital 2", latitude=13.0, longitude=80.0
            )
            HospitalCapacity.objects.create(hospital=hospital)

        await seed()
        police = await make_user("wsp_police", Role.TRAFFIC_POLICE)
        communicator, connected, code = await connect(
            "/ws/hospital/WSP2/", await token_for(police)
        )
        self.assertFalse(connected)
        self.assertEqual(code, 4403)
        await communicator.disconnect()

    async def test_hospital_role_may_open_it_and_sees_patient_data(self):
        from apps.core.enums import TripStage

        @database_sync_to_async
        def seed():
            from apps.dispatch.models import EmergencyTrip
            from apps.fleet.models import EmergencyVehicle
            from apps.hospitals.models import Hospital, HospitalCapacity

            hospital = Hospital.objects.create(
                code="WSP3", name="WS Policy Hospital 3", latitude=13.0, longitude=80.0
            )
            HospitalCapacity.objects.create(hospital=hospital)
            vehicle = EmergencyVehicle.objects.create(
                callsign="WSP-1", latitude=13.0, longitude=80.0
            )
            EmergencyTrip.objects.create(
                vehicle=vehicle, destination_hospital=hospital,
                emergency_category="cardiac", stage=TripStage.TO_HOSPITAL,
                patient_age=64, patient_notes="Chest pain",
            )

        await seed()
        nurse = await make_user("wsp_nurse", Role.HOSPITAL)
        communicator, connected, _ = await connect("/ws/hospital/WSP3/", await token_for(nurse))
        self.assertTrue(connected)

        snapshot = await communicator.receive_json_from(timeout=5)
        inbound = snapshot["data"]["inbound"]
        self.assertEqual(len(inbound), 1)
        self.assertEqual(inbound[0]["patient_age"], 64)
        await communicator.disconnect()

    async def test_anonymous_cannot_open_the_signals_bridge(self):
        """Regression: it accepted phase_report, which writes controller state."""
        communicator, connected, code = await connect("/ws/signals/")
        self.assertFalse(connected)
        self.assertEqual(code, 4401)
        await communicator.disconnect()

    async def test_paramedic_cannot_command_traffic_signals(self):
        medic = await make_user("wsp_medic", Role.AMBULANCE)
        communicator, connected, code = await connect("/ws/signals/", await token_for(medic))
        self.assertFalse(connected)
        self.assertEqual(code, 4403)
        await communicator.disconnect()

    async def test_traffic_police_may_open_the_signals_bridge(self):
        police = await make_user("wsp_cop2", Role.TRAFFIC_POLICE)
        communicator, connected, _ = await connect("/ws/signals/", await token_for(police))
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_anonymous_cannot_open_the_ops_firehose(self):
        communicator, connected, code = await connect("/ws/ops/")
        self.assertFalse(connected)
        self.assertEqual(code, 4401)
        await communicator.disconnect()

    async def test_any_authenticated_role_may_open_ops(self):
        for role in (Role.TRAFFIC_POLICE, Role.HOSPITAL, Role.AMBULANCE, Role.DISPATCHER):
            user = await make_user(f"wsp_ops_{role}", role)
            communicator, connected, _ = await connect("/ws/ops/", await token_for(user))
            self.assertTrue(connected, f"{role} should be able to open /ws/ops/")
            await communicator.disconnect()

    async def test_drivers_socket_stays_public(self):
        """Layer 4's promise: a road user needs no account."""
        communicator, connected, _ = await connect("/ws/drivers/?lat=13.06&lon=80.25")
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_superuser_may_open_everything(self):
        root = await make_user("wsp_root", superuser=True)
        token = await token_for(root)
        for path in ("/ws/ops/", "/ws/signals/"):
            communicator, connected, _ = await connect(path, token)
            self.assertTrue(connected, path)
            await communicator.disconnect()


# ---------------------------------------------------------------------------
# Transport behaviour
# ---------------------------------------------------------------------------
class SocketTransportTests(TransactionTestCase):
    async def _ops(self):
        user = await make_user("wsp_tx", Role.DISPATCHER)
        communicator, connected, _ = await connect("/ws/ops/", await token_for(user))
        self.assertTrue(connected)
        await communicator.receive_json_from(timeout=5)  # snapshot
        return communicator

    async def test_frames_carry_a_monotonic_sequence(self):
        """Without it a client cannot tell a dropped frame from a quiet period."""
        communicator = await self._ops()
        await communicator.send_json_to({"type": "ping"})
        first = await communicator.receive_json_from(timeout=5)
        await communicator.send_json_to({"type": "ping"})
        second = await communicator.receive_json_from(timeout=5)

        self.assertIn("seq", first)
        self.assertEqual(second["seq"], first["seq"] + 1)
        self.assertIn("ts", first)
        await communicator.disconnect()

    async def test_read_only_socket_rejects_commands_explicitly(self):
        """Silently dropping a command leaves the client guessing."""
        user = await make_user("wsp_ro", Role.HOSPITAL)

        @database_sync_to_async
        def seed():
            from apps.hospitals.models import Hospital, HospitalCapacity

            hospital = Hospital.objects.create(
                code="WSRO", name="RO", latitude=13.0, longitude=80.0
            )
            HospitalCapacity.objects.create(hospital=hospital)

        await seed()
        communicator, connected, _ = await connect("/ws/hospital/WSRO/", await token_for(user))
        self.assertTrue(connected)
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({"type": "phase_report", "controller_id": "X"})
        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response["event"], "error")
        self.assertIn("read-only", response["data"]["detail"])
        await communicator.disconnect()

    async def test_subscription_filters_narrow_the_stream(self):
        from apps.core.realtime import broadcast_ops

        communicator = await self._ops()
        await communicator.send_json_to(
            {"type": "subscribe", "filters": {"event": ["road_event_created"]}}
        )
        confirmation = await communicator.receive_json_from(timeout=5)
        self.assertEqual(confirmation["event"], "subscribed")

        await database_sync_to_async(broadcast_ops)("trip_created", {"reference": "X"})
        await database_sync_to_async(broadcast_ops)("road_event_created", {"id": 1})

        received = await communicator.receive_json_from(timeout=5)
        self.assertEqual(received["event"], "road_event_created")
        await communicator.disconnect()

    async def test_vehicle_positions_are_coalesced(self):
        """Nine vehicles at 2 s is nothing; a fleet at 1 s across N dashboards is not."""
        from apps.core.realtime import broadcast_ops

        communicator = await self._ops()
        for speed in (10, 20, 30, 40):
            await database_sync_to_async(broadcast_ops)(
                "vehicle_position", {"callsign": "AMB-C", "speed_kmh": speed}
            )

        first = await communicator.receive_json_from(timeout=5)
        self.assertEqual(first["event"], "vehicle_position")
        # The rest fall inside the coalesce window and are dropped.
        self.assertTrue(await communicator.receive_nothing(timeout=0.5))
        await communicator.disconnect()

    async def test_distinct_vehicles_are_not_coalesced_together(self):
        """Coalescing is per key - one vehicle must not suppress another."""
        from apps.core.realtime import broadcast_ops

        communicator = await self._ops()
        await database_sync_to_async(broadcast_ops)(
            "vehicle_position", {"callsign": "AMB-A", "speed_kmh": 10}
        )
        await database_sync_to_async(broadcast_ops)(
            "vehicle_position", {"callsign": "AMB-B", "speed_kmh": 20}
        )

        first = await communicator.receive_json_from(timeout=5)
        second = await communicator.receive_json_from(timeout=5)
        self.assertEqual(
            {first["data"]["callsign"], second["data"]["callsign"]}, {"AMB-A", "AMB-B"}
        )
        await communicator.disconnect()

    async def test_notifications_reach_the_ops_stream(self):
        from apps.core.notifications import Notification, publish

        communicator = await self._ops()
        await database_sync_to_async(publish)(
            Notification(title="Test alert", severity="critical")
        )
        received = await communicator.receive_json_from(timeout=5)
        self.assertEqual(received["event"], "notification")
        self.assertEqual(received["data"]["title"], "Test alert")
        self.assertIn("id", received["data"])
        await communicator.disconnect()
