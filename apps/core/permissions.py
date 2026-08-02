"""API authorisation policies.

Every SEVPS endpoint declares one of these. Nothing is left to DRF's global
default by accident - :mod:`apps.core.api_policy` audits that at test time,
so a new endpoint cannot ship unprotected simply because its author forgot.

Two axes:

**Role** - what the caller is (traffic police, hospital, crew, dispatcher).
**Sensitivity** - what the data is. Patient-identifying fields are gated
separately from operational fields, so traffic control can run a corridor
without ever seeing a diagnosis.

Backward compatibility: ``IsOperator`` and ``IsHospitalStaff`` predate the
role registry and are used across ~22 call sites. They are kept as thin
role-aware subclasses so existing views continue to work unchanged.
"""
from __future__ import annotations

from rest_framework import permissions

from apps.core.roles import (
    DISPATCH_ROLES,
    OPERATIONAL_ROLES,
    TRAFFIC_ROLES,
    Role,
    has_role,
    user_roles,
)


class BaseRolePermission(permissions.BasePermission):
    """Grants access when the caller holds any of ``required_roles``.

    Set ``allow_safe_methods`` to keep reads open while restricting writes.
    """

    required_roles: tuple[str, ...] = ()
    allow_safe_methods: bool = False
    message = "Your role does not permit this action."

    def has_permission(self, request, view) -> bool:
        if self.allow_safe_methods and request.method in permissions.SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if not self.required_roles:
            return True
        return has_role(user, *self.required_roles)


# ---------------------------------------------------------------------------
# General policies
# ---------------------------------------------------------------------------
class ReadOnlyOrAuthenticated(BaseRolePermission):
    """Anyone may read; any authenticated principal may write.

    The weakest write policy in the platform. Appropriate only where the
    written data is not operationally or clinically sensitive.
    """

    allow_safe_methods = True


class IsAuthenticatedRole(BaseRolePermission):
    """An *operational* role required for every method, including reads.

    Used on endpoints exposing dispatch-sensitive or patient-identifying data,
    where anonymous read is not acceptable.

    Note what this is not: "any authenticated user". `public_users` is a real
    role - a citizen with an account who receives driver alerts and reports
    incidents - and its RoleSpec has always read "no operational or clinical
    access". Until this was pinned by the RBAC matrix, holding that role also
    granted live ambulance positions, active trips, emergency routes and the
    full analytics history, because an empty `required_roles` means "anyone
    signed in".

    Live fleet tracking is not public data even in aggregate: it discloses,
    in near real time, which streets an ambulance was dispatched to.

    `/api/v1/auth/me/` deliberately uses DRF's plain `IsAuthenticated` instead,
    so a public user can still read their own account.
    """

    required_roles = tuple(sorted(OPERATIONAL_ROLES))
    allow_safe_methods = False


class PublicRead(permissions.BasePermission):
    """Explicitly, deliberately public - read only.

    Reserved for data that must reach unauthenticated consumers: roadside
    display boards, navigation-app integrations, the health probe, and the
    driver-alert lookup a road user's phone performs. Declaring it explicitly
    (rather than relying on ``AllowAny``) is what lets the policy audit tell
    "public on purpose" apart from "forgot to set a permission".
    """

    def has_permission(self, request, view) -> bool:
        return request.method in permissions.SAFE_METHODS


class PublicDeviceRegistration(permissions.BasePermission):
    """Public, and deliberately writable - device self-registration only.

    A narrow exception to :class:`PublicRead`, needed because Layer 4's promise
    is that a road user's phone receives an approaching-ambulance warning
    *without an account*, and registering for one is a POST.

    What makes it safe is that the caller can only ever describe itself. The
    request carries a push endpoint the browser just minted and a coarse
    position; it names no other subject, reads nothing back, and grants no
    access to platform state. Existing use: driver position reporting. Phase 9
    use: push subscribe and unsubscribe.

    Views using this must accept only writes that are self-describing. Anything
    that reads operational data or acts on another entity belongs behind a role.
    """

    message = "This endpoint accepts device self-registration only."

    def has_permission(self, request, view) -> bool:
        return request.method in (*permissions.SAFE_METHODS, "POST")


# ---------------------------------------------------------------------------
# Role policies
# ---------------------------------------------------------------------------
class IsAdministrator(BaseRolePermission):
    required_roles = (Role.ADMIN,)
    message = "Administrator privileges are required."


class PublicReadTrafficWrite(BaseRolePermission):
    """Public infrastructure data: anyone may read, traffic control may write.

    Road geometry, signal locations and camera positions are equivalent to
    published OpenStreetMap data, and navigation integrations consume them
    without accounts. Changing them is a traffic-authority action.
    """

    required_roles = tuple(TRAFFIC_ROLES)
    allow_safe_methods = True


class PublicReadHospitalWrite(BaseRolePermission):
    """Hospital directory: capability is public, capacity is hospital-written."""

    required_roles = (Role.HOSPITAL, Role.ADMIN)
    allow_safe_methods = True


class IsTrafficPolice(BaseRolePermission):
    """Traffic control room - signals, corridors, road events, CV sweeps."""

    required_roles = tuple(TRAFFIC_ROLES)
    message = "Traffic control privileges are required."


class IsDispatcher(BaseRolePermission):
    """Emergency dispatch - opening, reassigning and cancelling responses."""

    required_roles = tuple(DISPATCH_ROLES)
    message = "Emergency dispatcher privileges are required."


class IsAmbulanceCrew(BaseRolePermission):
    """Ambulance crew - telemetry, assessment, stage changes."""

    required_roles = (Role.AMBULANCE, Role.DISPATCHER, Role.ADMIN)
    message = "Ambulance crew privileges are required."


class IsOperator(IsTrafficPolice):
    """Backward-compatible alias for the pre-RBAC operator policy.

    Retained because ~19 views reference it. New code should use
    :class:`IsTrafficPolice`.
    """


class IsHospitalStaff(BaseRolePermission):
    """Hospital staff, scoped to their own hospital where tenancy is configured."""

    required_roles = (Role.HOSPITAL, Role.ADMIN)
    message = "Hospital staff privileges are required."

    def has_object_permission(self, request, view, obj) -> bool:
        """Scope a hospital user to their own hospital, when that is configured.

        ``Hospital.staff_group`` is optional. When a deployment sets it, this
        becomes real multi-tenant isolation: only that group may write to that
        hospital, so one hospital cannot declare another's diversion. When it
        is unset - the single-tenant pilot default - any hospital-role user
        may act, because there is no tenancy to enforce and refusing everyone
        would simply make the feature unusable.
        """
        if request.method in permissions.SAFE_METHODS or request.user.is_superuser:
            return True

        hospital = getattr(obj, "hospital", obj)
        staff_group = getattr(hospital, "staff_group", None)
        if staff_group is None:
            return has_role(request.user, Role.HOSPITAL, Role.ADMIN)
        return request.user.groups.filter(pk=staff_group.pk).exists()


class IsCrewForVehicle(BaseRolePermission):
    """Crew may only push telemetry for a vehicle they are assigned to.

    A dispatcher or administrator may act for any vehicle; a driver may not
    move another crew's ambulance on the map. ``EmergencyVehicle.assigned_to``
    is optional - unset means the pilot has not bound crews to vehicles yet,
    and any crew member may report.
    """

    required_roles = (Role.AMBULANCE, Role.DISPATCHER, Role.ADMIN)
    message = "You are not assigned to this vehicle."

    def has_object_permission(self, request, view, obj) -> bool:
        if request.method in permissions.SAFE_METHODS:
            return True
        if has_role(request.user, Role.DISPATCHER, Role.ADMIN):
            return True
        assigned = getattr(obj, "assigned_to_id", None)
        return assigned is None or assigned == request.user.id


def role_summary(user) -> dict:
    """Capability snapshot returned by ``/api/v1/auth/me/``."""
    roles = user_roles(user)
    from apps.core.roles import may_view_clinical_data

    return {
        "roles": sorted(roles),
        "is_superuser": bool(user.is_superuser) if user.is_authenticated else False,
        "is_staff": bool(user.is_staff) if user.is_authenticated else False,
        "capabilities": {
            "clinical_data": may_view_clinical_data(user),
            "traffic_control": has_role(user, *TRAFFIC_ROLES),
            "dispatch_control": has_role(user, *DISPATCH_ROLES),
            "admin_site": bool(user.is_staff) if user.is_authenticated else False,
        },
    }
