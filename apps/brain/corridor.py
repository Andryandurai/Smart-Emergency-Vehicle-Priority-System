"""Green corridor planning (Layer 2 "Green Corridor Planning", feature 4.1).

Planning answers *which* signals matter and *when* each must turn - it does
not touch a controller.  Actuation is Layer 3 (:mod:`apps.dispatch.corridor`),
which takes this plan, resolves conflicts between competing vehicles and
issues the hold/release commands.

The timing model per signal:

    t_arrival   the moment the vehicle reaches the stop line (from the route)
    t_clear     t_arrival - clearance, when the conflicting movements must
                already be stopped so the queue in front has dissipated
    t_green     t_clear - amber/all-red transition of the current phase
    t_release   t_arrival + release margin, when normal timing resumes

Turning a signal green too early is not free: it holds cross traffic for no
reason and, at a busy junction, the queue it builds can spill back and block
the corridor itself.  So the plan arms each signal as late as it safely can.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from apps.core.enums import PriorityLevel

#: Seconds of green needed before arrival to flush the queue ahead of the
#: vehicle.  Scales with how much traffic is actually sitting there.
BASE_CLEARANCE_S = 8.0
MAX_CLEARANCE_S = 35.0
#: Amber + all-red transition the controller needs before it can flip.
TRANSITION_S = 5.0
#: Keep the corridor green this long after the predicted arrival, to absorb
#: ETA error rather than dropping the light in the vehicle's face.
RELEASE_MARGIN_S = 6.0

#: Level 3 gets priority only where it actually helps (congested junctions);
#: Level 4 never preempts.
MIN_CONGESTION_FOR_MODERATE = 0.35


@dataclass
class SignalPlanEntry:
    intersection_id: int
    signal_id: int
    controller_id: str
    seconds_to_arrival: float
    arrival_at: object
    green_at: object
    release_at: object
    hold_duration_s: float
    clearance_s: float
    reason: str
    congestion_index: float = 0.0

    def as_dict(self) -> dict:
        return {
            "intersection_id": self.intersection_id,
            "signal_id": self.signal_id,
            "controller_id": self.controller_id,
            "seconds_to_arrival": round(self.seconds_to_arrival, 1),
            "arrival_at": self.arrival_at,
            "green_at": self.green_at,
            "release_at": self.release_at,
            "hold_duration_s": round(self.hold_duration_s, 1),
            "clearance_s": round(self.clearance_s, 1),
            "congestion_index": round(self.congestion_index, 3),
            "reason": self.reason,
        }


@dataclass
class CorridorPlan:
    trip_id: int
    priority_level: int
    entries: list[SignalPlanEntry] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def skipped_signal_ids(self) -> set[int]:
        """Signals still ahead on the route but not preempted *this tick*.

        The distinction matters to the actuator: a skipped signal is still on
        the route, so an outstanding request for it should be left alone
        rather than cancelled and recreated on the next GPS fix.
        """
        return {s["signal_id"] for s in self.skipped if s.get("signal_id")}

    def as_dict(self) -> dict:
        return {
            "trip_id": self.trip_id,
            "priority_level": self.priority_level,
            "signals": [e.as_dict() for e in self.entries],
            "skipped": self.skipped,
            "signal_count": len(self.entries),
        }


def _clearance_for(congestion_index: float, lanes: int) -> float:
    """Longer clearance where more vehicles have to physically get out of the way."""
    queue_factor = congestion_index * (1.0 + 0.25 * max(0, lanes - 1))
    return min(MAX_CLEARANCE_S, BASE_CLEARANCE_S + 40.0 * queue_factor)


def plan_corridor(trip, progress=None, *, lookahead_s: float | None = None) -> CorridorPlan:
    """Determine which signals on this trip's route need preemption, and when.

    ``progress`` is the vehicle's current :class:`~apps.brain.eta.RouteProgress`;
    it is recomputed if not supplied.
    """
    from apps.brain.eta import compute_progress, upcoming_signal_arrivals
    from apps.network.models import RoadSegment, TrafficSignal

    plan = CorridorPlan(trip_id=trip.id, priority_level=trip.priority_level)
    route_plan = getattr(trip, "active_route", None)
    if route_plan is None:
        return plan

    if trip.priority_level == PriorityLevel.NON_CRITICAL:
        plan.skipped.append({"reason": "Level 4 transport - no signal priority granted"})
        return plan

    lookahead = lookahead_s or settings.SEVPS["GREEN_CORRIDOR_LOOKAHEAD_S"]
    progress = progress or compute_progress(route_plan, trip.vehicle.point)
    if not progress.is_on_route:
        plan.skipped.append(
            {"reason": f"vehicle is {progress.offset_from_route_m:.0f} m off the planned route"}
        )
        return plan

    arrivals = upcoming_signal_arrivals(route_plan, progress, lookahead)
    if not arrivals:
        return plan

    signals = {
        s.intersection_id: s
        for s in TrafficSignal.objects.select_related("intersection").filter(
            intersection_id__in=[a["intersection_id"] for a in arrivals]
        )
    }
    segments = {
        s.id: s
        for s in RoadSegment.objects.filter(id__in=[a["segment_id"] for a in arrivals]).only(
            "id", "congestion_index", "lanes"
        )
    }

    now = timezone.now()
    for arrival in arrivals:
        signal = signals.get(arrival["intersection_id"])
        if signal is None:
            continue

        ok, why = signal.can_preempt_now()
        if not ok:
            plan.skipped.append(
                {
                    "intersection_id": arrival["intersection_id"],
                    "signal_id": signal.id,
                    "reason": why,
                }
            )
            continue

        segment = segments.get(arrival["segment_id"])
        congestion = segment.congestion_index if segment else 0.0
        lanes = segment.lanes if segment else 2

        if (
            trip.priority_level == PriorityLevel.MODERATE
            and congestion < MIN_CONGESTION_FOR_MODERATE
        ):
            plan.skipped.append(
                {
                    "intersection_id": arrival["intersection_id"],
                    "signal_id": signal.id,
                    "reason": "Level 3 and junction is free-flowing - priority not needed",
                }
            )
            continue

        seconds_away = arrival["seconds_away"]
        clearance = _clearance_for(congestion, lanes)
        arrival_at = now + timezone.timedelta(seconds=seconds_away)
        green_at = arrival_at - timezone.timedelta(seconds=clearance + TRANSITION_S)
        release_at = arrival_at + timezone.timedelta(seconds=RELEASE_MARGIN_S)

        # Never plan a green in the past, and never exceed the safety cap on
        # how long conflicting traffic may be held.
        if green_at < now:
            green_at = now
        hold_s = min(
            settings.SEVPS["SIGNAL_MAX_HOLD_S"],
            max(1.0, (release_at - green_at).total_seconds()),
        )

        plan.entries.append(
            SignalPlanEntry(
                intersection_id=arrival["intersection_id"],
                signal_id=signal.id,
                controller_id=signal.controller_id,
                seconds_to_arrival=seconds_away,
                arrival_at=arrival_at,
                green_at=green_at,
                release_at=green_at + timezone.timedelta(seconds=hold_s),
                hold_duration_s=hold_s,
                clearance_s=clearance,
                reason=(
                    f"Priority level {trip.priority_level} vehicle arriving in "
                    f"{seconds_away:.0f}s; junction congestion {congestion:.0%}"
                ),
                congestion_index=congestion,
            )
        )

    return plan
