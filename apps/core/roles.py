"""Role definitions for SEVPS role-based access control.

One registry, used by three consumers that must never disagree:

* :mod:`apps.core.permissions` - REST and WebSocket authorisation
* ``manage.py seed_users``     - group provisioning
* the JWT token claims        - so a client can render its own UI by role
  without a second round trip

Roles are Django ``Group`` rows. Membership is the *only* thing that grants a
role; ``is_superuser`` additionally implies every role, because an
administrator locked out of their own platform during an incident is a worse
failure than an over-broad grant.

Legacy group names are aliased rather than renamed. A deployment that already
has ``operators``/``paramedics`` groups keeps working after upgrade, which is
the whole point of an incremental migration.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoleSpec:
    key: str
    label: str
    description: str
    #: May read patient-identifying clinical data (age, notes, caller number).
    clinical_access: bool = False
    #: May command traffic infrastructure (signals, corridors, road events).
    traffic_control: bool = False
    #: May open, reassign and cancel emergency responses.
    dispatch_control: bool = False
    #: Group names from earlier releases that still map to this role.
    aliases: tuple[str, ...] = field(default_factory=tuple)


class Role:
    """Canonical group names. Import these, never hard-code the strings."""

    ADMIN = "administrators"
    TRAFFIC_POLICE = "traffic_police"
    HOSPITAL = "hospital_staff"
    AMBULANCE = "ambulance_drivers"
    DISPATCHER = "dispatchers"
    PUBLIC = "public_users"


ROLES: dict[str, RoleSpec] = {
    Role.ADMIN: RoleSpec(
        key=Role.ADMIN,
        label="Administrator",
        description="Full platform administration, including the Django admin site.",
        clinical_access=True,
        traffic_control=True,
        dispatch_control=True,
    ),
    Role.TRAFFIC_POLICE: RoleSpec(
        key=Role.TRAFFIC_POLICE,
        label="Traffic Police",
        description=(
            "Traffic control room. Signal preemption override, corridor release, "
            "road events, camera sweeps, hotspot analysis."
        ),
        # Deliberately NOT clinical: a traffic controller needs to know a
        # vehicle's priority and position, not the patient's diagnosis.
        clinical_access=False,
        traffic_control=True,
        aliases=("operators",),
    ),
    Role.HOSPITAL: RoleSpec(
        key=Role.HOSPITAL,
        label="Hospital Staff",
        description=(
            "Receiving hospital. Live bed capacity, diversion status, "
            "acknowledging inbound patients."
        ),
        clinical_access=True,
        aliases=("hospital",),
    ),
    Role.AMBULANCE: RoleSpec(
        key=Role.AMBULANCE,
        label="Ambulance Driver / Paramedic",
        description=(
            "Ambulance crew. Telemetry, patient assessment, hospital "
            "confirmation, trip stage changes."
        ),
        clinical_access=True,
        aliases=("paramedics",),
    ),
    Role.DISPATCHER: RoleSpec(
        key=Role.DISPATCHER,
        label="Emergency Dispatcher",
        description=(
            "Emergency call centre. Opens responses, assigns vehicles, "
            "reassigns and cancels trips."
        ),
        clinical_access=True,
        dispatch_control=True,
    ),
    Role.PUBLIC: RoleSpec(
        key=Role.PUBLIC,
        label="Public User",
        description=(
            "Registered road user. Receives driver alerts and reports "
            "incidents; no operational or clinical access."
        ),
    ),
}

#: legacy group name -> canonical role key
ALIASES: dict[str, str] = {
    alias: spec.key for spec in ROLES.values() for alias in spec.aliases
}

ALL_ROLES: tuple[str, ...] = tuple(ROLES)


def canonical(group_name: str) -> str:
    """Map a possibly-legacy group name onto its canonical role key."""
    return ALIASES.get(group_name, group_name)


def user_roles(user) -> set[str]:
    """Every role a user holds, with legacy group names resolved.

    A superuser implicitly holds every role; see the module docstring for why.
    """
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        return set(ALL_ROLES)

    names = {canonical(n) for n in user.groups.values_list("name", flat=True)}
    return names & set(ALL_ROLES)


def has_role(user, *roles: str) -> bool:
    """True when the user holds at least one of ``roles``."""
    if not roles:
        return bool(user and user.is_authenticated)
    return bool(user_roles(user) & set(roles))


def _roles_with(attribute: str) -> frozenset[str]:
    return frozenset(k for k, spec in ROLES.items() if getattr(spec, attribute))


#: Roles cleared to see patient-identifying information.
CLINICAL_ROLES = _roles_with("clinical_access")
#: Roles cleared to command traffic infrastructure.
TRAFFIC_ROLES = _roles_with("traffic_control")
#: Roles cleared to open and close emergency responses.
DISPATCH_ROLES = _roles_with("dispatch_control")


def may_view_clinical_data(user) -> bool:
    """Gate for patient age, clinical notes and caller identity.

    Traffic control genuinely does not need this to run a green corridor, so
    it does not get it - data minimisation, not distrust.
    """
    return has_role(user, *CLINICAL_ROLES)


def describe_roles() -> list[dict]:
    """Machine-readable role catalogue, served at /api/v1/auth/roles/."""
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "description": spec.description,
            "clinical_access": spec.clinical_access,
            "traffic_control": spec.traffic_control,
            "dispatch_control": spec.dispatch_control,
            "legacy_aliases": list(spec.aliases),
        }
        for spec in ROLES.values()
    ]
