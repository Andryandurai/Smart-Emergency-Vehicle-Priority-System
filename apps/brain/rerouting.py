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

from apps.brain.congestion import (
    blocked_segment_ids,
    build_forecaster,
    congested_segment_ids,
)
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
    #: The road ahead is passable but heavily congested. Surfaced separately
    #: from ``blocked`` because the console words the two differently: a
    #: closure is a fact, congestion is a judgement the crew may overrule.
    congested: bool = False

    def as_dict(self) -> dict:
        return {
            "should_reroute": self.should_reroute,
            "reason": self.reason,
            "gain_s": round(self.gain_s, 1),
            "blocked": self.blocked,
            "congested": self.congested,
        }


def _remaining_steps(route_plan, progress) -> list[dict]:
    """Plan steps the vehicle has not yet passed."""
    walked = 0.0
    remaining = []
    for step in route_plan.steps or []:
        walked += float(step.get("length_m", 0.0))
        if walked >= progress.distance_along_m:
            remaining.append(step)
    return remaining


def _live_remaining_cost_s(steps: list[dict], priority_level: int, fallback_s: float) -> float:
    """Re-price the rest of the current route against traffic as it is now.

    ``progress.remaining_s`` comes from the per-step timings frozen into the
    plan when it was computed. That is the right number for an ETA and the
    wrong one for a comparison: once a jam appears on the road ahead, the
    stored estimate still describes the clear run the router originally found,
    so every alternative scores a gain of roughly zero and the vehicle drives
    into the jam it was supposed to route around.

    The cost model here is deliberately the router's own, reproduced step for
    step from :func:`~apps.brain.router._materialise` - live forecast at the
    running offset, scaled by the priority speed advantage, plus the
    priority-scaled signal delay at the node being entered. Pricing the two
    sides of the comparison with different models is worse than not comparing
    at all: an earlier attempt divided the plan's priority-boosted speed by an
    unboosted forecast and reported a 165-second saving for switching to the
    identical road.
    """
    if not steps:
        return fallback_s

    from apps.brain import graph as graph_mod
    from apps.brain.router import _signal_delay_s, _speed_multiplier

    state = graph_mod.get_state()
    topo = graph_mod.get_topology()
    forecaster = build_forecaster(segment_ids=[s["segment_id"] for s in steps])
    multiplier = _speed_multiplier(priority_level)

    offset_s = 0.0
    priced = 0
    for step in steps:
        edge = state.edges.get(step["segment_id"])
        if edge is None:
            # A segment the graph no longer holds is not evidence the route
            # got faster; keep the plan's own figure for it.
            offset_s += max(0.0, float(step.get("travel_time_s", 0.0)))
            continue
        speed_kmh = forecaster.forecast(edge, offset_s).speed_kmh * multiplier
        travel_s = edge.length_m / max(1.0, speed_kmh / 3.6)
        travel_s += _signal_delay_s(priority_level, topo.node_delay_s.get(step["to_node"], 0.0))
        offset_s += travel_s
        priced += 1

    return offset_s if priced else fallback_s


def evaluate_trip(trip, *, reason: str = "periodic", force: bool = False) -> RerouteDecision:
    """Decide whether ``trip`` should be moved onto a new route."""
    route_plan = getattr(trip, "active_route", None)
    if route_plan is None or not trip.destination_latitude:
        return RerouteDecision(False, "trip has no active route")

    vehicle = trip.vehicle
    progress = compute_progress(route_plan, vehicle.point)
    destination = Point(trip.destination_latitude, trip.destination_longitude)

    blocked = set(blocked_segment_ids())
    remaining_steps = _remaining_steps(route_plan, progress)
    remaining = {step["segment_id"] for step in remaining_steps}
    route_is_blocked = bool(blocked & remaining)

    cfg = settings.SEVPS

    # Heavy congestion on the road ahead is the trigger the crew actually
    # notices, and it changes minute to minute - so it must be allowed to
    # break the replan cooldown the way a closure does. Without this the
    # vehicle drives into a jam that the graph already knew about, because
    # the last evaluation happened 30 seconds ago on a clear forecast.
    congested = congested_segment_ids(cfg["REROUTE_CONGESTION_THRESHOLD"])
    congested_ahead = congested & remaining
    route_is_congested = (
        len(congested_ahead) >= cfg["REROUTE_CONGESTION_MIN_SEGMENTS"]
    )

    if not (force or route_is_blocked or route_is_congested):
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

    # Both sides priced with the same model - see _live_remaining_cost_s.
    current_cost_s = _live_remaining_cost_s(
        remaining_steps, trip.priority_level, progress.remaining_s
    )
    gain = current_cost_s - candidate.total_duration_s

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

    # A congested road ahead lowers the bar rather than removing it. Any
    # positive gain is worth taking when the alternative is a jam, but a
    # slower detour is still a slower detour.
    threshold = (
        cfg["REROUTE_CONGESTED_MIN_GAIN_S"] if route_is_congested else cfg["REROUTE_MIN_GAIN_S"]
    )
    if gain >= threshold:
        if route_is_congested:
            detail = (
                f"heavy congestion on {len(congested_ahead)} segment(s) ahead - "
                f"faster route saves {gain / 60:.1f} min"
            )
        else:
            detail = f"alternative route saves {gain / 60:.1f} min ({reason})"
        return RerouteDecision(
            True,
            detail,
            gain_s=gain,
            route=candidate,
            congested=route_is_congested,
        )

    return RerouteDecision(
        False,
        f"current route still optimal (best alternative {gain:+.0f}s)",
        gain_s=gain,
        congested=route_is_congested,
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
