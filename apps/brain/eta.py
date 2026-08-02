"""ETA prediction and forward position projection (Layer 2).

Two questions are answered here, and both are asked continuously:

* *Where will this vehicle be in N seconds?*  - drives signal preemption
  timing and the driver-alert radius.
* *When will it reach the hospital / this intersection?*  - drives the
  hospital dashboard and the corridor schedule.

When a vehicle is on a planned route the projection follows the route
polyline using per-segment predicted speeds.  Only when there is no route
(or the vehicle has strayed off it) does it fall back to dead reckoning.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from apps.core.geo import Point, distance_to_polyline_m, point_along_polyline

log = logging.getLogger("sevps.brain.eta")

#: Beyond this perpendicular distance the vehicle is considered off-route.
OFF_ROUTE_THRESHOLD_M = 90.0


@dataclass
class RouteProgress:
    """Where a vehicle sits relative to its planned route."""

    distance_along_m: float
    remaining_m: float
    offset_from_route_m: float
    is_on_route: bool
    elapsed_offset_s: float       # route-time equivalent of the position
    remaining_s: float
    completion: float             # 0.0 .. 1.0

    def as_dict(self) -> dict:
        return {
            "distance_along_m": round(self.distance_along_m, 1),
            "remaining_m": round(self.remaining_m, 1),
            "offset_from_route_m": round(self.offset_from_route_m, 1),
            "is_on_route": self.is_on_route,
            "remaining_s": round(self.remaining_s, 1),
            "remaining_min": round(self.remaining_s / 60.0, 1),
            "completion": round(self.completion, 4),
        }


def _polyline(route_plan) -> list[Point]:
    return [Point(lat, lon) for lat, lon in route_plan.geometry]


def compute_progress(route_plan, position: Point) -> RouteProgress:
    """Locate ``position`` on a stored route plan and derive remaining time."""
    points = _polyline(route_plan)
    if len(points) < 2:
        return RouteProgress(0.0, 0.0, 0.0, False, 0.0, 0.0, 1.0)

    offset_m, along_m = distance_to_polyline_m(position, points)
    total_m = max(1.0, route_plan.total_distance_m)
    along_m = max(0.0, min(along_m, total_m))
    remaining_m = total_m - along_m

    elapsed_s, remaining_s = _time_for_distance(route_plan, along_m)
    return RouteProgress(
        distance_along_m=along_m,
        remaining_m=remaining_m,
        offset_from_route_m=offset_m,
        is_on_route=offset_m <= OFF_ROUTE_THRESHOLD_M,
        elapsed_offset_s=elapsed_s,
        remaining_s=remaining_s,
        completion=along_m / total_m,
    )


def _time_for_distance(route_plan, distance_along_m: float) -> tuple[float, float]:
    """Convert a distance along the route into (elapsed, remaining) seconds.

    Walks the stored per-step timings rather than assuming a constant speed,
    so a slow congested step correctly dominates the remaining estimate.
    """
    steps = route_plan.steps or []
    if not steps:
        total_s = route_plan.total_duration_s
        total_m = max(1.0, route_plan.total_distance_m)
        elapsed = total_s * (distance_along_m / total_m)
        return elapsed, max(0.0, total_s - elapsed)

    walked = 0.0
    for step in steps:
        length = float(step.get("length_m", 0.0))
        if walked + length >= distance_along_m:
            span = max(1e-6, length)
            frac = (distance_along_m - walked) / span
            enter = float(step.get("enter_offset_s", 0.0))
            exit_ = float(step.get("exit_offset_s", enter))
            elapsed = enter + (exit_ - enter) * max(0.0, min(1.0, frac))
            return elapsed, max(0.0, route_plan.total_duration_s - elapsed)
        walked += length

    return route_plan.total_duration_s, 0.0


def project_vehicle(vehicle, seconds_ahead: float) -> tuple[Point, str]:
    """Predict a vehicle's position ``seconds_ahead`` from now.

    Returns ``(point, method)`` where method is ``"route"`` or
    ``"dead_reckoning"`` so callers can weight their confidence.
    """
    trip = vehicle.active_trip
    plan = getattr(trip, "active_route", None) if trip else None
    if plan is not None:
        progress = compute_progress(plan, vehicle.point)
        if progress.is_on_route:
            target_offset_s = progress.elapsed_offset_s + max(0.0, seconds_ahead)
            distance = _distance_for_time(plan, target_offset_s)
            return point_along_polyline(_polyline(plan), distance), "route"
    return vehicle.project_position(seconds_ahead), "dead_reckoning"


def _distance_for_time(route_plan, offset_s: float) -> float:
    """Inverse of :func:`_time_for_distance`."""
    steps = route_plan.steps or []
    if not steps:
        total_s = max(1e-6, route_plan.total_duration_s)
        return route_plan.total_distance_m * min(1.0, offset_s / total_s)

    walked = 0.0
    for step in steps:
        enter = float(step.get("enter_offset_s", 0.0))
        exit_ = float(step.get("exit_offset_s", enter))
        length = float(step.get("length_m", 0.0))
        if exit_ >= offset_s:
            span = max(1e-6, exit_ - enter)
            frac = (offset_s - enter) / span
            return walked + length * max(0.0, min(1.0, frac))
        walked += length
    return route_plan.total_distance_m


def eta_for_trip(trip) -> dict | None:
    """Current ETA payload for a trip, or None when it has no active route."""
    plan = getattr(trip, "active_route", None)
    if plan is None:
        return None

    vehicle = trip.vehicle
    progress = compute_progress(plan, vehicle.point)

    # A stationary vehicle on a route would otherwise report a shrinking ETA
    # forever; hold the estimate and flag it instead.
    stalled = vehicle.speed_kmh < 3.0 and progress.completion < 0.98
    eta_at = timezone.now() + timezone.timedelta(seconds=progress.remaining_s)

    return {
        "trip_id": trip.id,
        "route_plan_id": plan.id,
        "eta": eta_at,
        "remaining_s": round(progress.remaining_s, 1),
        "remaining_min": round(progress.remaining_s / 60.0, 1),
        "remaining_m": round(progress.remaining_m, 1),
        "completion": round(progress.completion, 4),
        "is_on_route": progress.is_on_route,
        "off_route_by_m": round(progress.offset_from_route_m, 1),
        "is_stalled": stalled,
        "speed_kmh": round(vehicle.speed_kmh, 1),
    }


#: A junction stays in the corridor plan for this long *after* its predicted
#: arrival time.  Without the grace period, a telemetry gap that spans a
#: junction's green window would drop it from the plan before it was ever
#: armed - and the ambulance would meet a red light.  ETA error of a few
#: seconds is normal, so the plan must tolerate arriving late.
ARRIVAL_GRACE_S = 12.0


def upcoming_signal_arrivals(route_plan, progress: RouteProgress, horizon_s: float) -> list[dict]:
    """Signalised intersections the vehicle will reach within ``horizon_s``.

    This is the trigger list for Layer 3: each entry says which controller to
    preempt and how many seconds remain before the vehicle gets there.  A
    slightly negative ``seconds_away`` means the vehicle is predicted to be at
    the junction right now, which still warrants a green.
    """
    results = []
    for step in route_plan.steps or []:
        if not step.get("is_signalised_exit"):
            continue
        exit_offset = float(step.get("exit_offset_s", 0.0))
        seconds_away = exit_offset - progress.elapsed_offset_s
        if -ARRIVAL_GRACE_S <= seconds_away <= horizon_s:
            results.append(
                {
                    "intersection_id": step["to_node"],
                    "seconds_away": round(max(0.0, seconds_away), 1),
                    "segment_id": step["segment_id"],
                    "is_overdue": seconds_away < 0,
                }
            )
    results.sort(key=lambda item: item["seconds_away"])
    return results
