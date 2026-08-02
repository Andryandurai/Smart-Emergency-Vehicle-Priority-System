"""Emergency vehicle priority ranking (feature 4.6).

When two ambulances need the same intersection within seconds of each other,
somebody has to lose.  This module makes that decision explicit, deterministic
and auditable rather than leaving it to whichever request arrived first.

The score is deliberately interpretable - a traffic authority must be able to
explain, after the fact, why vehicle A was given the green and vehicle B held.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.core.enums import PriorityLevel

#: Severity dominates everything else: a Level 1 cardiac case outranks any
#: number of Level 3 transfers regardless of distance or ETA.
SEVERITY_WEIGHT = {
    PriorityLevel.CRITICAL: 1000.0,
    PriorityLevel.HIGH: 600.0,
    PriorityLevel.MODERATE: 250.0,
    PriorityLevel.NON_CRITICAL: 50.0,
}


@dataclass
class PriorityScore:
    trip_id: int
    vehicle_callsign: str
    priority_level: int
    score: float
    factors: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "trip_id": self.trip_id,
            "vehicle": self.vehicle_callsign,
            "priority_level": self.priority_level,
            "score": round(self.score, 2),
            "factors": {k: round(v, 2) for k, v in self.factors.items()},
        }


def score_trip(trip, *, seconds_to_conflict: float | None = None) -> PriorityScore:
    """Compute the priority score for one active trip.

    Factors, all additive on top of the severity band:

    * **urgency**    - how close the vehicle already is to its destination;
                       finishing a nearly-complete run frees the corridor.
    * **imminence**  - how soon it needs the contested intersection.
    * **impedance**  - how badly traffic is already hurting it; a vehicle
                       stuck in a jam benefits more from priority than one
                       already running freely.
    * **patient**    - explicit clinical deterioration flag from Layer 6.
    """
    severity = SEVERITY_WEIGHT.get(trip.priority_level, 50.0)
    factors: dict[str, float] = {"severity": severity}

    plan = getattr(trip, "active_route", None)
    remaining_s = plan.total_duration_s if plan else 0.0
    if plan is not None:
        from apps.brain.eta import compute_progress

        progress = compute_progress(plan, trip.vehicle.point)
        remaining_s = progress.remaining_s
        # Near the end of a run: finish it and release the corridor.
        factors["urgency"] = 120.0 * progress.completion
        # A vehicle crawling relative to normal emergency speed is being hurt
        # by traffic, so priority buys it more than it buys a free-running one.
        factors["impedance"] = 80.0 * max(0.0, min(1.0, 1.0 - trip.vehicle.speed_kmh / 40.0))

    if seconds_to_conflict is not None:
        # Sooner arrival wins; decays to zero over two minutes.
        factors["imminence"] = 150.0 * max(0.0, 1.0 - seconds_to_conflict / 120.0)

    if getattr(trip, "patient_deteriorating", False):
        factors["patient_deterioration"] = 200.0

    # Long remaining runs are slightly deprioritised at any single junction -
    # they will get many more chances than a vehicle two blocks from arrival.
    factors["remaining_penalty"] = -min(60.0, remaining_s / 60.0 * 4.0)

    return PriorityScore(
        trip_id=trip.id,
        vehicle_callsign=trip.vehicle.callsign,
        priority_level=trip.priority_level,
        score=sum(factors.values()),
        factors=factors,
    )


def rank_trips(trips, *, conflict_times: dict[int, float] | None = None) -> list[PriorityScore]:
    """Rank concurrent trips, highest priority first."""
    conflict_times = conflict_times or {}
    scores = [score_trip(t, seconds_to_conflict=conflict_times.get(t.id)) for t in trips]
    scores.sort(key=lambda s: (-s.score, s.priority_level, s.trip_id))
    return scores


def resolve_intersection_conflict(competing: list[tuple[object, float]]) -> tuple[object, list]:
    """Decide who gets the green at a contested intersection.

    ``competing`` is ``[(trip, seconds_away), ...]``.  Returns the winning trip
    and the full ranking so the decision can be logged and shown in the ops
    dashboard.
    """
    if not competing:
        return None, []
    trips = [t for t, _ in competing]
    conflict_times = {t.id: secs for t, secs in competing}
    ranking = rank_trips(trips, conflict_times=conflict_times)
    winner = next((t for t in trips if t.id == ranking[0].trip_id), None)
    return winner, ranking
