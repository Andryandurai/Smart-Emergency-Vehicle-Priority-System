"""Phase 2 tests: JWT lifecycle, RBAC matrix, PHI redaction, policy audit.

The RBAC matrix is table-driven on purpose. Asserting permissions view by
view scales badly and tends to test the roles that were remembered; a table
forces every (endpoint, role) pair to have a declared expectation, and a new
role or endpoint shows up as a missing entry rather than silent absence.
"""
from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from apps.core.api_policy import audit_api_permissions, policy_summary
from apps.core.roles import Role, has_role, may_view_clinical_data, user_roles


def make_user(username: str, *roles: str, **kwargs) -> User:
    user = User.objects.create_user(username, password="pw", **kwargs)
    for role in roles:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


# ---------------------------------------------------------------------------
# Role registry
# ---------------------------------------------------------------------------
class RoleRegistryTests(TestCase):
    def test_legacy_group_names_still_resolve(self):
        """An upgraded deployment keeps working before seed_users is re-run.

        Checked on ``hospital`` -> ``hospital_staff``. This used to check
        ``operators``, whose role (traffic police) has since been retired -
        and with the role gone the alias resolves to nothing, which is the
        correct outcome for a retired role but tests nothing about aliasing.
        """
        legacy = make_user("legacy", "hospital")
        self.assertIn(Role.HOSPITAL, user_roles(legacy))
        self.assertTrue(has_role(legacy, Role.HOSPITAL))

    def test_a_retired_roles_group_grants_nothing(self):
        """`operators` was traffic police. The role is gone; so is the grant."""
        stale = make_user("stale_op", "operators")
        self.assertEqual(user_roles(stale), set())

    def test_legacy_paramedics_group_resolves(self):
        crew = make_user("oldcrew", "paramedics")
        self.assertIn(Role.AMBULANCE, user_roles(crew))

    def test_superuser_holds_every_role(self):
        root = make_user("root", is_superuser=True, is_staff=True)
        self.assertEqual(user_roles(root), set(user_roles(root)) | {Role.ADMIN})
        self.assertTrue(has_role(root, Role.HOSPITAL))
        self.assertTrue(has_role(root, Role.ADMIN))

    def test_anonymous_holds_no_roles(self):
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(user_roles(AnonymousUser()), set())
        self.assertFalse(may_view_clinical_data(AnonymousUser()))

    def test_a_role_without_clinical_clearance_is_refused(self):
        """Data minimisation still has teeth after the role retirement.

        This used to check the traffic police, whose whole point was holding
        operational authority without clinical access. That role is gone, so
        the property is pinned on the public role instead - otherwise nothing
        would be asserting that `clinical_access=False` means anything.
        """
        road_user = make_user("uncleared", Role.PUBLIC)
        self.assertFalse(may_view_clinical_data(road_user))

    def test_clinical_roles_have_clearance(self):
        for role in (Role.HOSPITAL, Role.AMBULANCE, Role.ADMIN):
            user = make_user(f"u-{role}", role)
            self.assertTrue(may_view_clinical_data(user), role)

    def test_public_user_has_no_clearance(self):
        self.assertFalse(may_view_clinical_data(make_user("joe", Role.PUBLIC)))


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
class JWTTests(TestCase):
    def setUp(self):
        self.user = make_user("crew1", Role.AMBULANCE)

    def _obtain(self, username="crew1", password="pw"):
        return self.client.post(
            reverse("jwt-create"),
            data={"username": username, "password": password},
            content_type="application/json",
        )

    def test_obtain_returns_pair_and_identity(self):
        response = self._obtain()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        self.assertEqual(body["user"]["username"], "crew1")
        self.assertIn(Role.AMBULANCE, body["user"]["roles"])

    def test_token_carries_role_claims(self):
        from rest_framework_simplejwt.tokens import UntypedToken

        access = self._obtain().json()["access"]
        claims = UntypedToken(access)
        self.assertEqual(claims["username"], "crew1")
        self.assertIn(Role.AMBULANCE, claims["roles"])

    def test_refresh_token_is_set_as_httponly_cookie(self):
        """A refresh token in localStorage is a persistent account takeover."""
        response = self._obtain()
        cookie = response.cookies.get("sevps_refresh")
        self.assertIsNotNone(cookie)
        self.assertTrue(cookie["httponly"])

    def test_bad_credentials_rejected(self):
        self.assertEqual(self._obtain(password="wrong").status_code, 401)

    def test_access_token_authenticates_api_calls(self):
        access = self._obtain().json()["access"]
        response = self.client.get(
            reverse("auth-me"), HTTP_AUTHORIZATION=f"Bearer {access}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["auth_method"], "jwt")

    def test_refresh_works_from_cookie_alone(self):
        self._obtain()  # sets the cookie on self.client
        response = self.client.post(reverse("jwt-refresh"), data={}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.json())

    def test_logout_blacklists_the_refresh_token(self):
        refresh = self._obtain().json()["refresh"]
        self.assertEqual(
            self.client.post(
                reverse("jwt-logout"),
                data={"refresh": refresh},
                content_type="application/json",
            ).status_code,
            200,
        )
        # The blacklisted token must not be usable again.
        reuse = self.client.post(
            reverse("jwt-refresh"), data={"refresh": refresh}, content_type="application/json"
        )
        self.assertEqual(reuse.status_code, 401)

    def test_legacy_token_auth_still_works(self):
        """Existing field devices must not break during the migration."""
        response = self.client.post(
            reverse("api-token"),
            data={"username": "crew1", "password": "pw"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        key = response.json()["token"]
        me = self.client.get(reverse("auth-me"), HTTP_AUTHORIZATION=f"Token {key}")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["auth_method"], "legacy_token")

    def test_session_auth_still_works(self):
        self.client.force_login(self.user)
        me = self.client.get(reverse("auth-me"))
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["auth_method"], "session")


# ---------------------------------------------------------------------------
# RBAC matrix
# ---------------------------------------------------------------------------
class RBACMatrixTests(TestCase):
    """One declared expectation per (endpoint, role) pair."""

    @classmethod
    def setUpTestData(cls):
        cls.users = {
            "admin": make_user("m_admin", Role.ADMIN, is_staff=True, is_superuser=True),
            "crew": make_user("m_crew", Role.AMBULANCE),
            "medic": make_user("m_medic", Role.PARAMEDIC),
            "hospital": make_user("m_hosp", Role.HOSPITAL),
            "public": make_user("m_public", Role.PUBLIC),
        }

    #: (label, method, url, allowed roles). "anon" means unauthenticated.
    #:
    #: `public` is deliberately absent from every operational row. A registered
    #: road user is a subject of SEVPS, not an operator of it - the RoleSpec has
    #: always read "no operational or clinical access" - and until Phase 12 the
    #: permission layer did not enforce it. What a road user *does* get is the
    #: anonymous Layer 4 surface: /alerts/nearby/, /alerts/boards/live/ and the
    #: public GIS layers, all asserted in test_public_endpoints_remain_anonymous.
    MATRIX = [
        ("read trips", "get", "/api/v1/dispatch/trips/live/",
         {"admin", "crew", "medic", "hospital"}),
        ("corridor tick", "post", "/api/v1/dispatch/corridor/tick/", {"admin"}),
        ("rebuild graph", "post", "/api/v1/brain/network/rebuild/", {"admin"}),
        ("recompute hotspots", "post", "/api/v1/analytics/accident-hotspots/recompute/",
         {"admin"}),
        ("analytics summary", "get", "/api/v1/analytics/summary/",
         {"admin", "crew", "medic", "hospital"}),
        # The full alert table, carrying trip ids. The road-user surface is
        # /alerts/nearby/, which takes a position and returns only what is
        # approaching it.
        ("driver alert list", "get", "/api/v1/alerts/driver-alerts/",
         {"admin", "crew", "medic", "hospital"}),
    ]

    def _call(self, method, url, user=None):
        client = self.client_class()
        if user is not None:
            client.force_login(user)
        fn = getattr(client, method)
        return fn(url, data={}, content_type="application/json") if method == "post" else fn(url)

    def test_matrix(self):
        for label, method, url, allowed in self.MATRIX:
            with self.subTest(endpoint=label, role="anon"):
                status = self._call(method, url).status_code
                self.assertIn(status, (401, 403), f"{label}: anonymous should be denied")

            for role, user in self.users.items():
                with self.subTest(endpoint=label, role=role):
                    status = self._call(method, url, user).status_code
                    if role in allowed:
                        self.assertNotIn(
                            status, (401, 403), f"{label}: {role} should be allowed"
                        )
                    else:
                        self.assertIn(
                            status, (401, 403), f"{label}: {role} should be denied"
                        )

    def test_public_endpoints_remain_anonymous(self):
        """Display boards and road users have no credentials by design."""
        for url in (
            "/api/v1/health/",
            "/api/v1/alerts/boards/live/",
            "/api/v1/alerts/nearby/?lat=13.06&lon=80.25",
            "/api/v1/network/segments/geojson/?limit=1",
            "/api/v1/auth/roles/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


# ---------------------------------------------------------------------------
# PHI redaction
# ---------------------------------------------------------------------------
class ClinicalRedactionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from apps.core.enums import TripStage
        from apps.dispatch.models import EmergencyTrip
        from apps.fleet.models import EmergencyVehicle

        vehicle = EmergencyVehicle.objects.create(
            callsign="PHI-1", latitude=13.0, longitude=80.0
        )
        cls.trip = EmergencyTrip.objects.create(
            vehicle=vehicle,
            emergency_category="cardiac",
            stage=TripStage.TO_HOSPITAL,
            patient_age=57,
            patient_notes="Chest pain, ST elevation",
            caller_number="+919812345678",
            incident_address="12 Anna Salai",
        )

    def _trip_payload(self, user=None):
        if user is not None:
            self.client.force_login(user)
        response = self.client.get("/api/v1/dispatch/trips/live/")
        self.assertEqual(response.status_code, 200)
        return response.json()["trips"][0]

    def test_anonymous_cannot_read_trips_at_all(self):
        """Regression: this endpoint used to serve patient data unauthenticated."""
        self.assertIn(
            self.client.get("/api/v1/dispatch/trips/live/").status_code, (401, 403)
        )

    def test_clinical_roles_see_patient_data(self):
        for role in (Role.HOSPITAL, Role.AMBULANCE, Role.ADMIN):
            with self.subTest(role=role):
                payload = self._trip_payload(make_user(f"c-{role}", role))
                self.assertEqual(payload["patient_age"], 57)
                self.assertIn("Chest pain", payload["patient_notes"])
                self.client.logout()

    def _serialise_for(self, user):
        """Serialise the trip as ``user`` would see it.

        Exercised at the serializer rather than over HTTP, because after the
        traffic-police and dispatcher roles were retired there is no longer a
        role that can *reach* this endpoint without clinical clearance - every
        remaining operational role is cleared, and the public role is refused
        outright. The redaction code is still live and still has to be
        correct: an uncleared or unattributed serialisation must fail closed,
        and that is what this pins.
        """
        from apps.dispatch.serializers import EmergencyTripSerializer

        return EmergencyTripSerializer(self.trip, context={"user": user}).data

    def test_an_uncleared_caller_gets_redacted_patient_data(self):
        payload = self._serialise_for(make_user("phi-uncleared", Role.PUBLIC))
        self.assertIsNone(payload["patient_age"])
        self.assertIsNone(payload["patient_notes"])
        self.assertIsNone(payload["caller_number"])
        self.assertTrue(payload["clinical_data_redacted"])

    def test_an_unattributed_serialisation_fails_closed(self):
        """No user in context at all must redact, not expose."""
        payload = self._serialise_for(None)
        self.assertIsNone(payload["patient_notes"])
        self.assertTrue(payload["clinical_data_redacted"])

    def test_redaction_keeps_operational_fields(self):
        """Redaction must not break the corridor: priority and position stay."""
        payload = self._serialise_for(make_user("phi-uncleared2", Role.PUBLIC))
        self.assertEqual(payload["reference"], self.trip.reference)
        self.assertEqual(payload["priority_level"], self.trip.priority_level)
        self.assertIsNotNone(payload["vehicle_latitude"])

    def test_serialisation_without_a_user_fails_closed(self):
        from apps.dispatch.serializers import EmergencyTripSerializer

        data = EmergencyTripSerializer(self.trip).data
        self.assertIsNone(data["patient_notes"])
        self.assertTrue(data["clinical_data_redacted"])


# ---------------------------------------------------------------------------
# Policy audit
# ---------------------------------------------------------------------------
class APIPolicyTests(TestCase):
    def test_every_endpoint_declares_a_policy(self):
        """No endpoint may rely on the DRF global default by omission."""
        undeclared = [r for r in audit_api_permissions() if r.is_undeclared]
        self.assertEqual(
            undeclared,
            [],
            "These endpoints have no explicit permission_classes:\n"
            + "\n".join(f"  {r.pattern} -> {r.view}" for r in undeclared),
        )

    def test_no_undocumented_public_endpoints(self):
        """Making an endpoint public must be a visible, reviewed decision."""
        undocumented = [r for r in audit_api_permissions() if r.is_undocumented_public]
        self.assertEqual(
            undocumented,
            [],
            "Public endpoints missing from PUBLIC_READ_ENDPOINTS:\n"
            + "\n".join(f"  {r.name or '(unnamed)'} {r.pattern}" for r in undocumented),
        )

    def test_summary_reports_healthy(self):
        self.assertTrue(policy_summary()["healthy"])
