"""Dynamic route replanning (Layer 2, feature 4.10).

A route is a prediction, and predictions go stale.  Replanning is triggered by
three different things, all funnelled through :func:`evaluate_trip`:

* a **road event** appears on or near the active route (accident, closure,
  waterlogging, a public gathering);
* the vehicle **strays off** the planned route (driver's local knowledge, a
  blocked turn, a GPS-invisible diversion);
* the **forecast changes** enough that another path is now materially faster.

Replanning is not free: it invalidates the green corridor already armed ahead
of the vehicle and it confuses the crew.  So a new route must beat the current
one by a real margin (``REROUTE_MIN_GAIN_S``), and cannot be considered more
often than ``REROUTE_MIN_INTERVAL_S`` - except when the current route is
physically blocked, where the gain test is skipped entirely.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from apps.brain.congestion import blocked_segment_ids
from apps.brain.eta import compute_progress
from apps.brain.router import Route, RouteNotFound, route_between
from apps.core.geo import Point

log = logging.getLogger("sevps.brain.rerouting")


@dataclass
class RerouteDecision:
    should_reroute: bool
    reason: str
    gain_s: float = 0.0
    route: Route | None = None
    blocked: bool = False

    def as_dict(self) -> dict:
        return {
            "should_reroute": self.should_reroute,
            "reason": self.reason,
            "gain_s": round(self.gain_s, 1),
            "blocked": self.blocked,
        }


def _remaining_segment_ids(route_plan, progress) -> list[int]:
    """Segment ids the vehicle has not yet passed."""
    walked = 0.0
    remaining = []
    for step in route_plan.steps or []:
        walked += float(step.get("length_m", 0.0))
        if walked >= progress.distance_along_m:
            remaining.append(step["segment_id"])
    return remaining


def evaluate_trip(trip, *, reason: str = "periodic", force: bool = False) -> RerouteDecision:
    """Decide whether ``trip`` should be moved onto a new route."""
    route_plan = getattr(trip, "active_route", None)
    if route_plan is None or not trip.destination_latitude:
        return RerouteDecision(False, "trip has no active route")

    vehicle = trip.vehicle
    progress = compute_progress(route_plan, vehicle.point)
    destination = Point(trip.destination_latitude, trip.destination_longitude)

    blocked = set(blocked_segment_ids())
    remaining = set(_remaining_segment_ids(route_plan, progress))
    route_is_blocked = bool(blocked & remaining)

    cfg = settings.SEVPS
    if not (force or route_is_blocked):
        if not progress.is_on_route:
            reason = "vehicle has left the planned route"
        elif route_plan.computed_at and (
            timezone.now() - route_plan.computed_at
        ).total_seconds() < cfg["REROUTE_MIN_INTERVAL_S"]:
            return RerouteDecision(False, "replan cooldown active")

    try:
        candidate = route_between(
            vehicle.point,
            destination,
            priority_level=trip.priority_level,
            avoid_segment_ids=blocked,
            allow_contraflow=trip.allow_contraflow,
        )
    except RouteNotFound as exc:
        log.warning("reroute failed for trip %s: %s", trip.id, exc)
        return RerouteDecision(False, f"no alternative route available ({exc})", blocked=route_is_blocked)

    gain = progress.remaining_s - candidate.total_duration_s

    if route_is_blocked:
        return RerouteDecision(
            True,
            f"planned route is blocked ({reason})",
            gain_s=gain,
            route=candidate,
            blocked=True,
        )
    if not progress.is_on_route:
        return RerouteDecision(
            True, "vehicle is off the planned route", gain_s=gain, route=candidate
        )
    if gain >= cfg["REROUTE_MIN_GAIN_S"]:
        return RerouteDecision(
            True,
            f"alternative route saves {gain / 60:.1f} min ({reason})",
            gain_s=gain,
            route=candidate,
        )

    return RerouteDecision(
        False, f"current route still optimal (best alternative {gain:+.0f}s)", gain_s=gain
    )


def reassess_active_trips(*, reason: str = "network change") -> list[dict]:
    """Re-evaluate every in-flight trip - called when the network changes.

    Applied automatically when a road event is reported or cleared, which is
    what makes 4.10 reactive rather than merely periodic.
    """
    from apps.dispatch.models import EmergencyTrip
    from apps.dispatch.orchestrator import apply_new_route

    outcomes = []
    trips = EmergencyTrip.objects.active().select_related("vehicle")
    for trip in trips:
        decision = evaluate_trip(trip, reason=reason)
        if decision.should_reroute and decision.route is not None:
            apply_new_route(trip, decision.route, reason=decision.reason)
        outcomes.append({"trip_id": trip.id, **decision.as_dict()})
    if outcomes:
        log.info("reassessed %d active trip(s) after: %s", len(outcomes), reason)
    return outcomes
