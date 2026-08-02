"""Declared access policy for every REST endpoint, and an audit that proves it.

"Protect every API" is easy to claim and hard to keep true: the twentieth
endpoint added next quarter is the one that ships with the wrong permission.
So instead of trusting review, this module makes the property machine-checked.

:func:`audit_api_permissions` walks the live URL conf, resolves the permission
classes actually attached to each view, and flags anything that is:

* using DRF's global default by omission (no explicit declaration), or
* publicly readable without appearing in :data:`PUBLIC_READ_ENDPOINTS`.

``apps/core/tests_policy.py`` fails the build on either. Making an endpoint
public therefore requires a deliberate edit to the allowlist below, in a diff
a reviewer can see.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Endpoints that are public *on purpose*, with the reason recorded.
#:
#: Every entry here is a decision to serve data without credentials. They are
#: all read-only and none exposes patient-identifying information.
PUBLIC_READ_ENDPOINTS: dict[str, str] = {
    "api-health": "Load balancer and uptime probes must not need credentials.",
    "api-info": "Capability advertisement; used by clients to detect backends.",
    "api-token": "Legacy DRF token issuance - credentials are the payload.",
    "jwt-create": "Token issuance - credentials are the payload.",
    "jwt-refresh": "Token rotation - the refresh token is the credential.",
    "jwt-verify": "Token validity check - the token is the credential.",
    "jwt-logout": (
        "Sign-out must succeed even when the access token has already "
        "expired, otherwise a stale session cannot be ended."
    ),
    "api-root": "DRF router index - endpoint names only, no data.",
    "auth-roles": "Static role catalogue; helps clients render login UI.",
    # --- Layer 4: road users and roadside infrastructure ------------------
    "alerts-nearby": (
        "A road user's phone must receive an approaching-ambulance warning "
        "without holding an account. This is the core Layer 4 promise."
    ),
    "alerts-position": (
        "Device position reporting for alert targeting; stores a coarse "
        "geohash cell only, never linked to an identity."
    ),
    "displayboard-live": "Roadside VMS signs and city displays poll without credentials.",
    # --- Public network state ---------------------------------------------
    "segment-geojson": "Base road network geometry; equivalent to public OSM data.",
    "brain-route": "Public route planning; no operational state is disclosed.",
    "brain-route-compare": "Algorithm comparison endpoint; diagnostic only.",
    "brain-forecast": "Aggregate congestion forecast; no vehicle or patient data.",
    "brain-network-summary": "Graph health counters; no operational state.",
    "hospital-recommend": (
        "Category-to-hospital recommendation. Takes a location and a category, "
        "returns public hospital capability - no patient record is involved."
    ),
    "hospital-rule-lookup": "Clinical rule catalogue; published reference data.",
    "dispatch-priority-profiles": "Static Layer 6 documentation of the four levels.",
}

#: URL names whose *list/read* exposes patient-identifying data and therefore
#: must never be anonymous, even for GET.
CLINICAL_ENDPOINTS: frozenset[str] = frozenset(
    {
        "trip-list", "trip-detail",
        "hospitalalert-list", "hospitalalert-detail",
        "recommendationlog-list", "recommendationlog-detail",
        "directive-list", "directive-detail",
    }
)


@dataclass
class EndpointReport:
    name: str
    pattern: str
    view: str
    permissions: list[str]
    declared: bool
    public: bool

    @property
    def is_undeclared(self) -> bool:
        """Relying on the DRF global default rather than saying so."""
        return not self.declared

    @property
    def is_undocumented_public(self) -> bool:
        return self.public and self.name not in PUBLIC_READ_ENDPOINTS


def _permission_names(view_cls) -> tuple[list[str], bool]:
    """Permission class names for a view, and whether it declared them itself."""
    declared = "permission_classes" in vars(view_cls)

    # ViewSets can also declare per-action permissions via @action decorators;
    # treat any explicit mention in the class body as a declaration.
    if not declared:
        for attr in vars(view_cls).values():
            if getattr(attr, "kwargs", None) and "permission_classes" in attr.kwargs:
                declared = True
                break

    classes = getattr(view_cls, "permission_classes", []) or []
    return [c.__name__ for c in classes], declared


def audit_api_permissions() -> list[EndpointReport]:
    """Walk the live URL conf and report the effective policy per endpoint."""
    from django.urls import get_resolver

    reports: list[EndpointReport] = []

    def walk(resolver, prefix=""):
        for pattern in resolver.url_patterns:
            route = prefix + str(pattern.pattern)
            if hasattr(pattern, "url_patterns"):
                walk(pattern, route)
                continue
            if not route.startswith("api/v1/"):
                continue

            callback = pattern.callback
            view_cls = getattr(callback, "cls", None) or getattr(
                callback, "view_class", None
            )
            if view_cls is None:
                # Function-based views decorated with @api_view expose their
                # permissions on the wrapper.
                classes = [
                    c.__name__ for c in getattr(callback, "permission_classes", []) or []
                ]
                declared = bool(classes)
                view_name = getattr(callback, "__name__", str(callback))
            else:
                classes, declared = _permission_names(view_cls)
                view_name = view_cls.__name__

            reports.append(
                EndpointReport(
                    name=pattern.name or "",
                    pattern=route,
                    view=view_name,
                    permissions=classes,
                    declared=declared,
                    public=any(
                        c in {"AllowAny", "PublicRead"} for c in classes
                    ),
                )
            )

    walk(get_resolver())
    return reports


def policy_summary() -> dict:
    """Human-readable summary, surfaced at ``/api/v1/auth/policy/``."""
    reports = audit_api_permissions()
    undeclared = [r for r in reports if r.is_undeclared]
    undocumented = [r for r in reports if r.is_undocumented_public]

    return {
        "endpoints": len(reports),
        "explicitly_declared": len(reports) - len(undeclared),
        "undeclared": [
            {"name": r.name, "pattern": r.pattern, "view": r.view} for r in undeclared
        ],
        "public_documented": len(
            [r for r in reports if r.public and r.name in PUBLIC_READ_ENDPOINTS]
        ),
        "public_undocumented": [
            {"name": r.name, "pattern": r.pattern, "view": r.view} for r in undocumented
        ],
        "healthy": not undeclared and not undocumented,
    }
