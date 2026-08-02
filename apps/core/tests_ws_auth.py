"""WebSocket authentication tests.

The failure this guards against is specific and quiet: after a JWT migration
the REST API authenticates correctly while every socket connects as
``AnonymousUser``, because Channels' stock stack reads the session cookie and
a bearer-token client has no session. Nothing errors - the sockets simply
stop knowing who is on them, and any role check on a consumer fails open.
"""
from __future__ import annotations

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import Group, User
from django.test import TransactionTestCase

from apps.core.roles import Role
from sevps.asgi import application


@database_sync_to_async
def make_user(username: str, *roles: str) -> User:
    user = User.objects.create_user(username, password="pw")
    for role in roles:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


@database_sync_to_async
def issue_token(user) -> str:
    from apps.core.jwt import SEVPSTokenObtainPairSerializer

    return str(SEVPSTokenObtainPairSerializer.get_token(user).access_token)


class WebSocketJWTTests(TransactionTestCase):
    async def test_jwt_in_query_string_authenticates_the_socket(self):
        user = await make_user("ws_crew", Role.AMBULANCE)
        token = await issue_token(user)

        communicator = WebsocketCommunicator(application, f"/ws/ops/?token={token}")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        viewer = (await communicator.receive_json_from(timeout=5))["data"]["viewer"]
        self.assertTrue(viewer["authenticated"])
        self.assertEqual(viewer["username"], "ws_crew")
        self.assertEqual(viewer["auth_method"], "jwt")
        self.assertIn(Role.AMBULANCE, viewer["roles"])
        await communicator.disconnect()

    async def test_jwt_in_authorization_header_authenticates_the_socket(self):
        user = await make_user("ws_hdr", Role.DISPATCHER)
        token = await issue_token(user)

        communicator = WebsocketCommunicator(
            application,
            "/ws/ops/",
            headers=[(b"authorization", f"Bearer {token}".encode())],
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        viewer = (await communicator.receive_json_from(timeout=5))["data"]["viewer"]
        self.assertTrue(viewer["authenticated"])
        self.assertEqual(viewer["username"], "ws_hdr")
        await communicator.disconnect()

    async def test_garbage_token_yields_anonymous_not_an_error(self):
        """A bad token must not crash the handshake; the policy then decides.

        Checked on the drivers socket, which is legitimately public - a road
        user with a stale token still needs their ambulance warning.
        """
        communicator = WebsocketCommunicator(
            application, "/ws/drivers/?lat=13.06&lon=80.25&token=not-a-jwt"
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        viewer = (await communicator.receive_json_from(timeout=5))["data"]["viewer"]
        self.assertFalse(viewer["authenticated"])
        await communicator.disconnect()

    async def test_garbage_token_is_refused_on_a_protected_socket(self):
        """The same bad token must not open the control-room firehose."""
        communicator = WebsocketCommunicator(application, "/ws/ops/?token=not-a-jwt")
        connected, code = await communicator.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4401)
        await communicator.disconnect()

    async def test_public_feed_still_connects_without_a_token(self):
        """Layer 4's promise: a road user needs no account."""
        communicator = WebsocketCommunicator(application, "/ws/drivers/?lat=13.06&lon=80.25")
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        viewer = (await communicator.receive_json_from(timeout=5))["data"]["viewer"]
        self.assertFalse(viewer["authenticated"])
        self.assertEqual(viewer["roles"], [])
        await communicator.disconnect()

    async def test_session_auth_is_not_clobbered_by_the_jwt_layer(self):
        """Regression: middleware nesting order.

        With the layers the wrong way round the session stack overwrites the
        JWT identity with AnonymousUser. Here we assert the reverse case -
        a session-authenticated socket with no token keeps its user.
        """
        communicator = WebsocketCommunicator(application, "/ws/ops/")
        communicator.scope["user"] = await make_user("ws_session", Role.HOSPITAL)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        await communicator.disconnect()

    async def test_clinical_data_is_redacted_on_the_socket_for_traffic_police(self):
        """Redaction must hold on the socket, not just over REST."""
        from apps.core.enums import TripStage

        @database_sync_to_async
        def seed():
            from apps.dispatch.models import EmergencyTrip
            from apps.fleet.models import EmergencyVehicle

            vehicle = EmergencyVehicle.objects.create(
                callsign="WS-PHI", latitude=13.0, longitude=80.0
            )
            EmergencyTrip.objects.create(
                vehicle=vehicle,
                emergency_category="cardiac",
                stage=TripStage.TO_HOSPITAL,
                patient_age=61,
                patient_notes="Confidential",
            )

        await seed()
        police = await make_user("ws_cop", Role.TRAFFIC_POLICE)
        token = await issue_token(police)

        communicator = WebsocketCommunicator(application, f"/ws/ops/?token={token}")
        await communicator.connect()
        snapshot = await communicator.receive_json_from(timeout=5)
        trips = snapshot["data"]["trips"]
        self.assertTrue(trips)
        self.assertIsNone(trips[0]["patient_notes"])
        self.assertTrue(trips[0]["clinical_data_redacted"])
        await communicator.disconnect()

    async def test_clinical_role_sees_patient_data_on_the_socket(self):
        from apps.core.enums import TripStage

        @database_sync_to_async
        def seed():
            from apps.dispatch.models import EmergencyTrip
            from apps.fleet.models import EmergencyVehicle

            vehicle = EmergencyVehicle.objects.create(
                callsign="WS-PHI2", latitude=13.0, longitude=80.0
            )
            EmergencyTrip.objects.create(
                vehicle=vehicle,
                emergency_category="cardiac",
                stage=TripStage.TO_HOSPITAL,
                patient_age=61,
                patient_notes="Visible to clinicians",
            )

        await seed()
        medic = await make_user("ws_medic", Role.AMBULANCE)
        token = await issue_token(medic)

        communicator = WebsocketCommunicator(application, f"/ws/ops/?token={token}")
        await communicator.connect()
        snapshot = await communicator.receive_json_from(timeout=5)
        trips = snapshot["data"]["trips"]
        self.assertTrue(trips)
        self.assertEqual(trips[0]["patient_age"], 61)
        await communicator.disconnect()
