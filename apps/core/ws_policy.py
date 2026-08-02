"""Declared access policy for every WebSocket endpoint, and an audit that proves it.

Phase 2 gave the REST surface a machine-checked policy. The sockets got
authentication but never authorisation, and the gap was not theoretical: a
probe against the running server found ``/ws/hospital/<code>/`` serving patient
age and clinical notes to an anonymous connection, and ``/ws/signals/``
accepting traffic-controller commands from one.

The failure mode is specific to sockets. A REST view without a permission
class is obvious in review; a consumer has no equivalent, and its snapshot
method often bypasses the serializer that would have redacted the payload.
So consumers declare a policy the same way views do, and
``apps/core/tests_ws_policy.py`` fails the build if any registered consumer
does not.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.core.roles import Role


@dataclass(frozen=True)
class ConsumerPolicy:
    """Who may open a socket, and what they may do once it is open."""

    name: str
    #: Empty tuple with ``allow_anonymous=False`` means "any authenticated user".
    required_roles: tuple[str, ...] = ()
    #: Public feeds. Every one of these is a deliberate decision, recorded in
    #: ``reason`` and asserted by the audit.
    allow_anonymous: bool = False
    #: True when the client may send messages that mutate server state. Those
    #: sockets always need a role - a read-only leak is bad, a write is worse.
    accepts_commands: bool = False
    #: Payloads carry patient-identifying data and must be redacted per role.
    carries_clinical_data: bool = False
    reason: str = ""

    def permits(self, user) -> bool:
        from apps.core.roles import has_role

        if user is None or not user.is_authenticated:
            return self.allow_anonymous
        if not self.required_roles:
            return True
        return has_role(user, *self.required_roles)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "required_roles": list(self.required_roles),
            "allow_anonymous": self.allow_anonymous,
            "accepts_commands": self.accepts_commands,
            "carries_clinical_data": self.carries_clinical_data,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# The policies
# ---------------------------------------------------------------------------
OPS_POLICY = ConsumerPolicy(
    name="ops",
    required_roles=(),  # any authenticated role
    allow_anonymous=False,
    carries_clinical_data=True,
    reason=(
        "The control-room firehose: every vehicle position, trip, corridor and "
        "disruption in the city. Operationally sensitive even with clinical "
        "fields redacted, so it requires a signed-in role."
    ),
)

VEHICLE_POLICY = ConsumerPolicy(
    name="vehicle",
    required_roles=(Role.AMBULANCE, Role.DISPATCHER, Role.TRAFFIC_POLICE, Role.ADMIN),
    accepts_commands=True,   # telemetry is pushed up this socket
    carries_clinical_data=True,
    reason=(
        "Onboard unit feed. Accepts telemetry, which moves a vehicle on the "
        "map and drives signal preemption - never anonymous. Traffic police "
        "are included so a controller can watch a corridor, and clinical "
        "fields are redacted for them by the serializer."
    ),
)

HOSPITAL_POLICY = ConsumerPolicy(
    name="hospital",
    required_roles=(Role.HOSPITAL, Role.DISPATCHER, Role.AMBULANCE, Role.ADMIN),
    carries_clinical_data=True,
    reason=(
        "Pre-arrival patient feed: emergency category, age, clinical notes and "
        "deterioration flag for identified inbound patients. This is the most "
        "clinically sensitive stream in the platform."
    ),
)

SIGNALS_POLICY = ConsumerPolicy(
    name="signals",
    required_roles=(Role.TRAFFIC_POLICE, Role.ADMIN),
    accepts_commands=True,
    reason=(
        "Bridge to physical traffic controllers. Receives green/release "
        "commands and accepts phase reports that write controller state, so "
        "it is restricted to traffic control."
    ),
)

DRIVERS_POLICY = ConsumerPolicy(
    name="drivers",
    allow_anonymous=True,
    accepts_commands=True,  # position reports, used only for alert targeting
    reason=(
        "Layer 4's core promise: a road user must receive an approaching-"
        "ambulance warning without holding an account. Carries no patient or "
        "vehicle-identifying data - only a warning, an ETA and a bearing. The "
        "position a client reports is stored as a coarse geohash cell for "
        "targeting and is never linked to an identity."
    ),
)

POLICIES: dict[str, ConsumerPolicy] = {
    policy.name: policy
    for policy in (OPS_POLICY, VEHICLE_POLICY, HOSPITAL_POLICY, SIGNALS_POLICY, DRIVERS_POLICY)
}


def policy_summary() -> list[dict]:
    """Surfaced at ``/api/v1/auth/policy/`` alongside the REST audit."""
    return [policy.as_dict() for policy in POLICIES.values()]
