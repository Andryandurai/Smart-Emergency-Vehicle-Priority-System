"""Shared pytest fixtures for SEVPS.

Composed by dependency rather than by inheritance, which is the point of
adopting pytest alongside the existing unittest suite: a test that needs a
signalised junction, an ambulance and a hospital asks for three fixtures
instead of inheriting a base class that sets up all three plus eight things it
does not need.

Nothing here changes how the existing `TestCase` classes run. Fixtures are
opt-in; a `TestCase` that does not request one is unaffected.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User

from apps.core.roles import ALL_ROLES, Role

CHENNAI = (13.0604, 80.2496)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True, scope="session")
def _fast_password_hashing():
    """Use MD5 hashing for test accounts.

    Django's default PBKDF2 does ~600,000 iterations, which is exactly right in
    production and ruinous in a suite that creates seven users per test: the
    RBAC matrix alone spent about four seconds per case hashing passwords
    nobody checks. Swapping the hasher took it from roughly twenty minutes to
    under one.

    Safe because it applies only under pytest, and no test asserts anything
    about hash strength - `apps/core/tests_auth.py` exercises the login flow,
    not the KDF.
    """
    from django.conf import settings

    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def _quiet_logging(caplog):
    """Keep dispatch telemetry out of failure output.

    A failing assertion buried under 200 lines of "vehicle AMB-3 position
    updated" is materially harder to read, and the logs are reproducible by
    running the test with `-o log_cli=1`.
    """
    import logging

    caplog.set_level(logging.CRITICAL, logger="sevps")


@pytest.fixture
def postgis_or_skip():
    """Skip a test that genuinely needs PostGIS rather than assert against
    the SQLite fallback and call it a spatial test."""
    from apps.core.spatial import postgis_available

    if not postgis_available():
        pytest.skip("PostGIS not available on this database")


# ---------------------------------------------------------------------------
# Users and roles
# ---------------------------------------------------------------------------
@pytest.fixture
def role_group(db):
    """Get-or-create a Django group for a role key."""

    def _make(role: str) -> Group:
        group, _ = Group.objects.get_or_create(name=role)
        return group

    return _make


@pytest.fixture
def make_user(db, role_group):
    """Build a user holding the given roles.

    Password is fixed and weak on purpose: these accounts exist for the length
    of one test transaction, and a random password would only make failures
    harder to reproduce by hand.
    """

    def _make(username: str, *roles: str, superuser: bool = False) -> User:
        if superuser:
            user = User.objects.create_superuser(username, password="test-pass-12345")
        else:
            user = User.objects.create_user(username, password="test-pass-12345")
        for role in roles:
            user.groups.add(role_group(role))
        return user

    return _make


@pytest.fixture
def users(make_user):
    """One user per role, plus an administrator and an anonymous placeholder.

    Keyed by role so a parametrised test can look one up by the role under
    test without a chain of if/elif.
    """
    built = {role: make_user(f"user_{role}", role) for role in ALL_ROLES}
    built["superuser"] = make_user("root", superuser=True)
    return built


@pytest.fixture
def api_client():
    """A DRF client. Unauthenticated unless `as_role` is used."""
    from rest_framework.test import APIClient

    return APIClient()


@pytest.fixture
def as_role(api_client, users):
    """Authenticate the API client as the holder of a role.

    Uses `force_authenticate` rather than a real JWT round trip: these tests
    are about *authorisation*, and re-exercising token issuance in each of
    ~250 matrix cells would triple the runtime while testing something
    apps/core/tests_auth.py already covers thoroughly.
    """

    def _login(role: str | None):
        if role is None:
            api_client.force_authenticate(user=None)
        else:
            api_client.force_authenticate(user=users[role])
        return api_client

    return _login


# ---------------------------------------------------------------------------
# Road network
# ---------------------------------------------------------------------------
@pytest.fixture
def junction(db):
    """A signalised intersection with a controller attached."""
    from apps.network.models import Intersection, TrafficSignal

    node = Intersection.objects.create(
        latitude=CHENNAI[0], longitude=CHENNAI[1], name="Anna Salai / Mount Rd",
        is_signalised=True,
    )
    TrafficSignal.objects.create(intersection=node, controller_id="TSC-001")
    return node


@pytest.fixture
def corridor(db, junction):
    """A short chain of segments through the signalised junction.

    Three nodes rather than two, because a routing or corridor test with a
    single edge cannot distinguish "picked the right path" from "had no
    choice".
    """
    from apps.network.models import Intersection, RoadSegment

    upstream = Intersection.objects.create(latitude=13.055, longitude=80.245, name="Upstream")
    downstream = Intersection.objects.create(latitude=13.066, longitude=80.255, name="Downstream")

    inbound = RoadSegment.objects.create(
        from_node=upstream, to_node=junction, name="Anna Salai (N)",
        length_m=600.0, latitude=13.058, longitude=80.247,
        geometry=[[13.055, 80.245], [CHENNAI[0], CHENNAI[1]]],
    )
    outbound = RoadSegment.objects.create(
        from_node=junction, to_node=downstream, name="Anna Salai (S)",
        length_m=550.0, latitude=13.063, longitude=80.252,
        geometry=[[CHENNAI[0], CHENNAI[1]], [13.066, 80.255]],
    )
    return {"upstream": upstream, "downstream": downstream,
            "inbound": inbound, "outbound": outbound, "junction": junction}


# ---------------------------------------------------------------------------
# Fleet, hospitals, trips
# ---------------------------------------------------------------------------
@pytest.fixture
def ambulance(db):
    from apps.fleet.models import EmergencyVehicle

    return EmergencyVehicle.objects.create(
        callsign="AMB-01", latitude=CHENNAI[0], longitude=CHENNAI[1],
    )


@pytest.fixture
def hospital(db):
    """A hospital with capability and capacity - all three rows.

    A Hospital without a HospitalCapacity is not a hospital the recommender
    can score, and a fixture that omits it produces tests that pass because
    nothing was ever a candidate.
    """
    from apps.hospitals.models import Hospital, HospitalCapability, HospitalCapacity

    site = Hospital.objects.create(
        code="APL", name="Apollo Main", latitude=13.0350, longitude=80.2510,
    )
    for facility in ("emergency_dept", "cardiac_icu", "cath_lab"):
        HospitalCapability.objects.create(hospital=site, facility=facility)
    HospitalCapacity.objects.create(
        hospital=site, emergency_beds_total=20, emergency_beds_available=16,
        icu_beds_total=10, icu_beds_available=3,
    )
    return site


@pytest.fixture
def trip(db, ambulance, hospital):
    from apps.core.enums import EmergencyCategory, TripStage
    from apps.dispatch.models import EmergencyTrip

    return EmergencyTrip.objects.create(
        vehicle=ambulance,
        emergency_category=EmergencyCategory.CARDIAC,
        priority_level=1,
        stage=TripStage.TO_SCENE,
        destination_hospital=hospital,
        patient_age=61,
        patient_notes="Chest pain, ST elevation on 12-lead.",
    )


@pytest.fixture
def clinical_trip(trip):
    """Alias that names *why* a test wants it: to check PHI redaction."""
    return trip
