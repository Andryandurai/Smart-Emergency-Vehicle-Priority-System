"""The authorisation matrix: every role against every protected endpoint.

Why this exists as one parametrised sweep rather than as tests scattered
through each app: authorisation is the property most likely to regress
silently. A serializer change or a new mixin can quietly open an endpoint, and
nothing fails - the feature still works, it just also works for people it
should not. Individual per-app tests check the cases their author thought of.
This checks *the whole product*, and it is expressed once.

Four security defects were found in earlier phases by probing the running
server by hand - PHI readable anonymously over REST, the same over WebSocket,
unauthenticated signal-controller commands, and hospital staff locked out of
their own hospital. This is the machine-checked version of that probing, so
the next one fails CI instead of needing to be noticed.

Read the tables below as the specification. If a change makes one of these
fail, the question is not "how do I fix the test" but "did I mean to change
who can do this".
"""
from __future__ import annotations

import pytest

from apps.core.roles import Role

pytestmark = [pytest.mark.rbac, pytest.mark.django_db]

#: Roles a request can arrive with. `None` is anonymous.
#: Every kind of caller the API can see. One entry per role - duplicates give
#: pytest colliding parametrisation ids (administrators0, administrators1)
#: and test the same thing repeatedly.
CALLERS = [None, Role.PUBLIC, Role.ADMIN, Role.HOSPITAL,
           Role.AMBULANCE, Role.PARAMEDIC, "superuser"]

ALLOWED = {200, 201, 202, 204}
DENIED = {401, 403}


def outcome(status: int) -> str:
    """Collapse a status into allowed / denied / other.

    404 and 405 are deliberately *not* folded into "denied". An endpoint that
    404s for an unauthorised caller is leaking less than one that 403s, but it
    is also not what the policy says, and treating them as equivalent hides
    routing mistakes - a typo'd URL would pass every denial assertion.
    """
    if status in ALLOWED:
        return "allowed"
    if status in DENIED:
        return "denied"
    return f"unexpected:{status}"


# ---------------------------------------------------------------------------
# Public reads.
#
# Each of these is in PUBLIC_READ_ENDPOINTS with a written reason. The test
# asserts the reason is honoured: a road user's phone, a roadside sign and a
# navigation integration hold no credentials, and Layer 4 depends on that.
# ---------------------------------------------------------------------------
PUBLIC_GETS = [
    "/api/v1/health/",
    "/api/v1/health/live/",
    "/api/v1/health/ready/",
    "/api/v1/info/",
    "/api/v1/auth/roles/",
    "/api/v1/network/segments/geojson/?limit=5",
    "/api/v1/network/gis/layers/",
    "/api/v1/network/gis/layers/road_network/",
    "/api/v1/network/gis/layers/hospitals/",
    "/api/v1/network/gis/layers/traffic_signals/",
    "/api/v1/network/gis/layers/road_closures/",
    "/api/v1/network/gis/basemaps/",
    "/api/v1/alerts/boards/live/",
    "/api/v1/hospitals/rules/catalogue/",
    "/api/v1/dispatch/priority-profiles/",
    "/api/v1/notify/vapid-key/",
]

# ---------------------------------------------------------------------------
# Reads that require *any* signed-in role. Not public: live vehicle positions,
# incident history and daily emergency volume are operational intelligence.
# ---------------------------------------------------------------------------
AUTHENTICATED_GETS = [
    "/api/v1/fleet/vehicles/live/",
    "/api/v1/dispatch/trips/live/",
    "/api/v1/network/gis/layers/emergency_vehicles/",
    "/api/v1/network/gis/layers/emergency_routes/",
    "/api/v1/network/gis/layers/accident_heatmap/",
    "/api/v1/analytics/summary/",
    "/api/v1/analytics/trends/",
    "/api/v1/analytics/demand/",
    "/api/v1/analytics/distribution/",
    "/api/v1/analytics/corridor-outcomes/",
    "/api/v1/analytics/response-distribution/",
    "/api/v1/analytics/hospital-load/",
    "/api/v1/analytics/export/",
    "/api/v1/analytics/export/daily.csv",
    "/api/v1/notify/inbox/",
    "/api/v1/notify/preferences/",
    "/api/v1/notify/subscriptions/",
    "/api/v1/notify/health/",
    "/api/v1/auth/me/",
]


@pytest.mark.parametrize("url", PUBLIC_GETS)
@pytest.mark.parametrize("role", CALLERS)
def test_public_reads_are_open_to_everyone(as_role, role, url):
    client = as_role(role)
    assert outcome(client.get(url).status_code) == "allowed"


@pytest.mark.parametrize("url", AUTHENTICATED_GETS)
def test_operational_reads_are_closed_to_anonymous(as_role, url):
    client = as_role(None)
    assert outcome(client.get(url).status_code) == "denied"


@pytest.mark.parametrize("url", AUTHENTICATED_GETS)
@pytest.mark.parametrize(
    "role",
    [Role.ADMIN, Role.HOSPITAL, Role.AMBULANCE, Role.PARAMEDIC],
)
def test_operational_reads_are_open_to_every_operational_role(as_role, role, url):
    client = as_role(role)
    assert outcome(client.get(url).status_code) == "allowed"


ADMIN_ONLY_GETS = [
    # Enumerates every endpoint and its permission class. Useful to a
    # reviewer, equally useful to someone probing for the one that is wrong.
    "/api/v1/auth/policy/",
]


@pytest.mark.parametrize("url", ADMIN_ONLY_GETS)
@pytest.mark.parametrize(
    "role", [None, Role.PUBLIC, Role.HOSPITAL, Role.AMBULANCE, Role.PARAMEDIC],
)
def test_admin_only_reads_are_closed_to_every_other_role(as_role, role, url):
    assert outcome(as_role(role).get(url).status_code) == "denied"


@pytest.mark.parametrize("url", ADMIN_ONLY_GETS)
@pytest.mark.parametrize("role", [Role.ADMIN, "superuser"])
def test_admin_only_reads_are_open_to_administrators(as_role, role, url):
    assert outcome(as_role(role).get(url).status_code) == "allowed"


def test_public_users_do_not_get_operational_data(as_role):
    """`public_users` is a real role - a citizen with an account. Holding it
    must not be the same as being staff."""
    client = as_role(Role.PUBLIC)
    denied = [url for url in AUTHENTICATED_GETS
              if outcome(client.get(url).status_code) != "denied"]
    # /auth/me/ is the exception: anyone signed in may read their own account.
    assert denied == ["/api/v1/auth/me/"], denied


# ---------------------------------------------------------------------------
# Writes. The table is the specification: which role may command what.
#
# Hospital staff deliberately do not appear in the traffic column, and no
# non-clinical role appears in the clinical one. That is data minimisation,
# not distrust.
#
# Traffic and dispatch authority used to belong to their own roles. Those were
# retired, and both now sit with the administrator - so `{Role.ADMIN}` in this
# table means "only an administrator", not "an administrator among others".
# ---------------------------------------------------------------------------
WRITE_CASES = [
    # (label, method, url, body, roles that must be allowed)
    (
        "release a green corridor",
        "post", "/api/v1/dispatch/trips/{trip}/corridor/release/",
        {"reason": "manual override"},
        {Role.ADMIN},
    ),
    (
        "declare hospital diversion",
        "post", "/api/v1/hospitals/hospitals/{hospital}/diversion/",
        {"is_on_diversion": True, "reason": "at capacity"},
        {Role.HOSPITAL, Role.ADMIN},
    ),
    (
        "update hospital capacity",
        "patch", "/api/v1/hospitals/hospitals/{hospital}/capacity/",
        {"emergency_beds_available": 9},
        {Role.HOSPITAL, Role.ADMIN},
    ),
    (
        "cancel an emergency response",
        "post", "/api/v1/dispatch/trips/{trip}/cancel/",
        {"reason": "stood down"},
        {Role.ADMIN},
    ),
    (
        "push vehicle telemetry",
        "post", "/api/v1/fleet/vehicles/{vehicle}/telemetry/",
        {"latitude": 13.06, "longitude": 80.25, "speed_kmh": 40},
        {Role.AMBULANCE, Role.PARAMEDIC, Role.ADMIN},
    ),
]


@pytest.mark.parametrize(
    "label,method,url,body,allowed_roles",
    WRITE_CASES,
    ids=[case[0] for case in WRITE_CASES],
)
@pytest.mark.parametrize("role", CALLERS)
def test_write_authorisation_matrix(
    as_role, trip, hospital, ambulance, label, method, url, body, allowed_roles, role
):
    client = as_role(role)
    target = url.format(trip=trip.id, hospital=hospital.id, vehicle=ambulance.id)
    result = outcome(getattr(client, method)(target, body, format="json").status_code)

    # A superuser holds every role by design - an administrator locked out of
    # their own platform mid-incident is a worse failure than an over-broad
    # grant. See apps/core/roles.py.
    should_be_allowed = role == "superuser" or role in allowed_roles

    assert result == ("allowed" if should_be_allowed else "denied"), (
        f"{role or 'anonymous'} -> {label}: got {result}"
    )


# ---------------------------------------------------------------------------
# Clinical data. The redaction rule, checked from the outside.
# ---------------------------------------------------------------------------
CLINICAL_FIELDS = ("patient_age", "patient_notes", "caller_number")

CLEARED = {Role.HOSPITAL, Role.AMBULANCE, Role.PARAMEDIC, Role.ADMIN, "superuser"}
# The administrator is cleared; only the public role and anonymous are not.
NOT_CLEARED = {Role.PUBLIC, None}


@pytest.mark.parametrize("role", sorted(CLEARED, key=str))
def test_cleared_roles_see_clinical_data(as_role, trip, role):
    response = as_role(role).get(f"/api/v1/dispatch/trips/{trip.id}/")
    assert response.status_code == 200
    assert response.json().get("patient_age") == 61


@pytest.mark.parametrize("role", sorted(NOT_CLEARED, key=str))
def test_uncleared_roles_never_see_clinical_data(as_role, trip, role):
    """Traffic police included. A controller needs priority and position to
    run a corridor, not a diagnosis."""
    response = as_role(role).get(f"/api/v1/dispatch/trips/{trip.id}/")
    if outcome(response.status_code) == "denied":
        return  # denied outright is a stronger guarantee than redacted
    body = response.json()
    for field in CLINICAL_FIELDS:
        assert not body.get(field), f"{role or 'anonymous'} could read {field}"
    assert body.get("clinical_data_redacted") is True


def test_an_uncleared_caller_cannot_reach_the_trip_list_at_all(as_role, trip):
    """The strongest form of the guarantee, and the one that now holds.

    This used to sign in as the traffic police - a role that could read the
    list but had no clinical clearance - and assert the notes were redacted
    out of it. That role is retired, and every role that can still reach this
    endpoint is cleared, so the property to pin is the outer one: an uncleared
    caller is refused the list entirely.
    """
    for role in (None, Role.PUBLIC):
        response = as_role(role).get("/api/v1/dispatch/trips/live/")
        assert outcome(response.status_code) == "denied", role


def test_a_list_serialised_without_context_still_redacts(trip):
    """Where a `many=True` serializer would quietly leak the whole table.

    No HTTP, deliberately: the risk is a serializer used somewhere that
    forgets to pass a request, and that cannot be reached through a view.
    """
    from apps.dispatch.models import EmergencyTrip
    from apps.dispatch.serializers import EmergencyTripSerializer

    body = str(EmergencyTripSerializer(EmergencyTrip.objects.all(), many=True).data)
    assert "Chest pain" not in body
    assert "ST elevation" not in body


# ---------------------------------------------------------------------------
# Device self-registration. Public and writable, which needs its own check.
# ---------------------------------------------------------------------------
def test_a_road_user_can_subscribe_to_alerts_without_an_account(api_client):
    """Layer 4's core promise. If this ever requires a login, the feature is
    gone for the people it exists for."""
    response = api_client.post(
        "/api/v1/notify/subscribe/",
        {"endpoint": "https://push.example.org/rbac-matrix",
         "keys": {"p256dh": "k", "auth": "a"}},
        format="json",
    )
    assert response.status_code in (200, 201)


def test_the_subscribe_endpoint_is_not_readable(api_client):
    """PublicDeviceRegistration permits POST only. A readable subscribe
    endpoint would enumerate every registered device."""
    assert api_client.get("/api/v1/notify/subscribe/").status_code == 405


def test_notification_test_send_cannot_target_another_user(as_role, users, db):
    from apps.notify.models import PushSubscription

    victim = users[Role.HOSPITAL]
    PushSubscription.objects.create(
        user=victim, endpoint="https://push.example.org/victim", p256dh="k", auth="a"
    )
    response = as_role(Role.ADMIN).post("/api/v1/notify/test/", {}, format="json")
    # No subscriptions of their own -> 409, and crucially not a delivery to
    # someone else's device.
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# The policy audit itself.
# ---------------------------------------------------------------------------
def test_no_endpoint_relies_on_the_global_default(db):
    """An endpoint with no declared permission is one whose author forgot."""
    from apps.core.api_policy import audit_api_permissions

    undeclared = [report.name or report.pattern
                  for report in audit_api_permissions() if report.is_undeclared]
    assert undeclared == []


def test_every_public_endpoint_has_a_written_reason(db):
    """Making something public requires editing an allowlist, in a diff a
    reviewer can see."""
    from apps.core.api_policy import audit_api_permissions

    undocumented = [report.name or report.pattern
                    for report in audit_api_permissions()
                    if report.is_undocumented_public]
    assert undocumented == []


def test_the_public_allowlist_has_no_stale_entries(db):
    """An allowlist entry for a deleted endpoint is a permission waiting to be
    silently reused by the next thing with that URL name."""
    from apps.core.api_policy import PUBLIC_READ_ENDPOINTS, audit_api_permissions

    live = {report.name for report in audit_api_permissions() if report.name}
    stale = sorted(set(PUBLIC_READ_ENDPOINTS) - live)
    assert stale == [], f"allowlisted but no longer routed: {stale}"
