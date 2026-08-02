"""Layer 3 - Smart Traffic Signal Control (green corridor actuation).

Takes the corridor *plan* produced by :mod:`apps.brain.corridor` and turns it
into real controller commands, resolving conflicts when two priority vehicles
want the same junction.

Design constraints that shape everything here:

* **Fail safe.**  A controller that cannot be reached leaves the junction on
  normal timing.  A preemption that is never explicitly released is force-
  released by :func:`tick_corridors` once its cap expires - a light stuck
  green is far more dangerous than a light that reverts too early.
* **Bounded holds.**  ``SIGNAL_MAX_HOLD_S`` caps how long conflicting traffic
  is held, regardless of what the plan asked for.
* **One winner per junction.**  Two green corridors crossing at the same
  intersection is a collision, so exactly one preemption may be active per
  signal; the loser is recorded as yielding, not silently dropped.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.brain.corridor import plan_corridor
from apps.core.enums import PreemptionState
from apps.core.realtime import broadcast_ops
from apps.dispatch.controllers import get_controller
from apps.dispatch.models import SignalPreemption

log = logging.getLogger("sevps.dispatch.corridor")


def sync_corridor(trip, progress=None) -> dict:
    """Bring the corridor for one trip in line with its current plan.

    Called on every position update.  Idempotent: safe to call repeatedly.
    """
    from apps.core.enums import TripStage

    if trip.stage in {TripStage.ARRIVED, TripStage.HANDOVER, TripStage.CANCELLED}:
        return release_corridor(trip, reason="trip complete")

    plan = plan_corridor(trip, progress)
    planned_by_signal = {entry.signal_id: entry for entry in plan.entries}

    with transaction.atomic():
        existing = {
            p.signal_id: p
            for p in SignalPreemption.objects.select_for_update()
            .filter(trip=trip, state__in=[PreemptionState.PLANNED, PreemptionState.ARMED, PreemptionState.ACTIVE])
            .select_related("signal", "signal__intersection")
        }

        created, updated, cancelled = [], [], []

        # 1. Upsert the plan.
        for signal_id, entry in planned_by_signal.items():
            preemption = existing.get(signal_id)
            if preemption is None:
                preemption = SignalPreemption.objects.create(
                    trip=trip,
                    signal_id=signal_id,
                    planned_green_at=entry.green_at,
                    planned_release_at=entry.release_at,
                    predicted_arrival_at=entry.arrival_at,
                    clearance_s=entry.clearance_s,
                    hold_duration_s=entry.hold_duration_s,
                    reason=entry.reason,
                )
                created.append(preemption)
            elif preemption.state != PreemptionState.ACTIVE:
                # Re-timing an armed request is fine; re-timing an active one
                # is not - the light is already green and the crew is committed.
                preemption.planned_green_at = entry.green_at
                preemption.planned_release_at = entry.release_at
                preemption.predicted_arrival_at = entry.arrival_at
                preemption.clearance_s = entry.clearance_s
                preemption.hold_duration_s = entry.hold_duration_s
                preemption.reason = entry.reason
                preemption.save(
                    update_fields=[
                        "planned_green_at", "planned_release_at", "predicted_arrival_at",
                        "clearance_s", "hold_duration_s", "reason", "updated_at",
                    ]
                )
                updated.append(preemption)

        # 2. Drop preemptions for signals that dropped off the route (reroute,
        #    or the vehicle has passed them).  A signal the planner merely
        #    *skipped* this tick - recovery window, free-flowing junction - is
        #    still ahead on the route, so its request is left standing rather
        #    than cancelled and recreated on the next GPS fix.
        skipped_signal_ids = plan.skipped_signal_ids
        for signal_id, preemption in existing.items():
            if signal_id in planned_by_signal or signal_id in skipped_signal_ids:
                continue
            _release_one(preemption, reason="signal no longer on route")
            cancelled.append(preemption)

    activated = _activate_due(trip)
    released = _release_passed(trip)

    return {
        "trip_id": trip.id,
        "planned": len(planned_by_signal),
        "created": len(created),
        "updated": len(updated),
        "cancelled": len(cancelled),
        "activated": activated,
        "released": released,
        "skipped": plan.skipped,
    }


def _activate_due(trip) -> list[dict]:
    """Send the green command for any preemption whose moment has come."""
    now = timezone.now()
    due = (
        SignalPreemption.objects.filter(
            trip=trip,
            state__in=[PreemptionState.PLANNED, PreemptionState.ARMED],
            planned_green_at__lte=now,
        )
        .select_related("signal", "signal__intersection", "trip", "trip__vehicle")
        .order_by("planned_green_at")
    )

    results = []
    for preemption in due:
        winner = _resolve_conflict(preemption)
        if winner is not preemption:
            preemption.state = PreemptionState.CANCELLED
            preemption.yielded_to = winner
            preemption.reason = (
                f"yielded to {winner.trip.vehicle.callsign} "
                f"(level {winner.trip.priority_level})"
            )
            preemption.save(update_fields=["state", "yielded_to", "reason", "updated_at"])
            results.append(
                {
                    "signal": preemption.signal.controller_id,
                    "status": "yielded",
                    "to": winner.trip.vehicle.callsign,
                }
            )
            continue

        hold_s = min(settings.SEVPS["SIGNAL_MAX_HOLD_S"], preemption.hold_duration_s)
        controller = get_controller(preemption.signal)
        outcome = controller.request_green(
            preemption.signal,
            duration_s=hold_s,
            context={
                "trip_reference": trip.reference,
                "priority_level": trip.priority_level,
                "vehicle": trip.vehicle.callsign,
                "reason": preemption.reason,
            },
        )

        preemption.controller_response = outcome.as_dict()
        if outcome.ok:
            preemption.state = PreemptionState.ACTIVE
            preemption.activated_at = timezone.now()
            preemption.planned_release_at = preemption.activated_at + timezone.timedelta(
                seconds=hold_s
            )
        else:
            preemption.state = PreemptionState.FAILED
            log.warning(
                "preemption failed at %s: %s", preemption.signal.controller_id, outcome.detail
            )
        preemption.save(
            update_fields=[
                "state", "activated_at", "planned_release_at",
                "controller_response", "updated_at",
            ]
        )
        results.append(
            {
                "signal": preemption.signal.controller_id,
                "status": preemption.state,
                "detail": outcome.detail,
                "hold_s": round(hold_s, 1),
            }
        )
    return results


def _resolve_conflict(preemption) -> SignalPreemption:
    """Pick the winning request when several trips want the same signal."""
    from apps.brain.priority import resolve_intersection_conflict

    competitors = list(
        SignalPreemption.objects.filter(
            signal_id=preemption.signal_id,
            state__in=[PreemptionState.PLANNED, PreemptionState.ARMED, PreemptionState.ACTIVE],
        )
        .exclude(pk=preemption.pk)
        .select_related("trip", "trip__vehicle")
    )
    if not competitors:
        return preemption

    # An already-active green wins by default: revoking it mid-manoeuvre would
    # strand a vehicle in the middle of an intersection.
    active = next((c for c in competitors if c.state == PreemptionState.ACTIVE), None)
    if active is not None:
        return active

    now = timezone.now()
    entrants = [preemption, *competitors]
    competing = [
        (
            p.trip,
            (p.predicted_arrival_at - now).total_seconds() if p.predicted_arrival_at else 60.0,
        )
        for p in entrants
    ]
    winning_trip, ranking = resolve_intersection_conflict(competing)
    if winning_trip is None:
        return preemption

    winner = next((p for p in entrants if p.trip_id == winning_trip.id), preemption)
    if winner is not preemption:
        broadcast_ops(
            "signal_conflict",
            {
                "signal_id": preemption.signal_id,
                "winner": winning_trip.vehicle.callsign,
                "ranking": [s.as_dict() for s in ranking],
            },
        )
    winner.priority_score = ranking[0].score if ranking else 0.0
    return winner


def _release_passed(trip) -> list[dict]:
    """Restore normal timing once the vehicle has cleared each junction."""
    now = timezone.now()
    active = (
        SignalPreemption.objects.filter(trip=trip, state=PreemptionState.ACTIVE)
        .select_related("signal", "signal__intersection")
    )

    released = []
    vehicle = trip.vehicle
    for preemption in active:
        intersection = preemption.signal.intersection
        distance = vehicle.distance_to(intersection)
        # Released either because the vehicle is measurably past the junction,
        # or because the hold cap has been reached.
        passed = distance > 60 and preemption.actual_arrival_at is not None
        expired = preemption.planned_release_at and now >= preemption.planned_release_at

        if distance <= 60 and preemption.actual_arrival_at is None:
            preemption.actual_arrival_at = now
            preemption.save(update_fields=["actual_arrival_at", "updated_at"])

        if passed or expired:
            _release_one(
                preemption,
                reason="vehicle cleared the junction" if passed else "hold window expired",
            )
            released.append(
                {
                    "signal": preemption.signal.controller_id,
                    "reason": preemption.reason,
                    "held_s": round(preemption.actual_hold_s or 0.0, 1),
                    "eta_error_s": (
                        round(preemption.eta_error_s, 1)
                        if preemption.eta_error_s is not None
                        else None
                    ),
                }
            )
    return released


def _release_one(preemption, *, reason: str) -> None:
    """Release a single preemption and restore normal signal timing."""
    if preemption.state == PreemptionState.ACTIVE:
        controller = get_controller(preemption.signal)
        outcome = controller.release(
            preemption.signal,
            context={"trip_reference": preemption.trip.reference, "reason": reason},
        )
        preemption.controller_response = outcome.as_dict()
        preemption.released_at = timezone.now()
        preemption.state = PreemptionState.RELEASED
    else:
        preemption.state = PreemptionState.CANCELLED

    preemption.reason = reason
    preemption.save(
        update_fields=["state", "released_at", "reason", "controller_response", "updated_at"]
    )


def release_corridor(trip, *, reason: str = "corridor released") -> dict:
    """Tear down every outstanding preemption for a trip."""
    open_preemptions = SignalPreemption.objects.filter(
        trip=trip,
        state__in=[PreemptionState.PLANNED, PreemptionState.ARMED, PreemptionState.ACTIVE],
    ).select_related("signal", "trip")

    count = 0
    for preemption in open_preemptions:
        _release_one(preemption, reason=reason)
        count += 1
    if count:
        log.info("released %d preemption(s) for trip %s: %s", count, trip.reference, reason)
    return {"trip_id": trip.id, "released": count, "reason": reason}


def tick_corridors() -> dict:
    """Safety sweep - run on a timer, independent of any GPS update.

    Without this, a vehicle that loses GPS mid-corridor would leave a junction
    held green indefinitely.  Anything past its release time, or past the
    absolute hold cap, is force-released here.
    """
    now = timezone.now()
    cap_s = settings.SEVPS["SIGNAL_MAX_HOLD_S"]

    stale = (
        SignalPreemption.objects.filter(state=PreemptionState.ACTIVE)
        .select_related("signal", "trip")
    )
    released = 0
    for preemption in stale:
        overdue = preemption.planned_release_at and now >= preemption.planned_release_at
        over_cap = (
            preemption.activated_at
            and (now - preemption.activated_at).total_seconds() > cap_s
        )
        if overdue or over_cap:
            _release_one(
                preemption,
                reason="hold cap reached (safety sweep)" if over_cap else "hold window expired",
            )
            released += 1

    # Plans that were never activated (vehicle rerouted, trip ended) expire too.
    expired = SignalPreemption.objects.filter(
        state__in=[PreemptionState.PLANNED, PreemptionState.ARMED],
        planned_release_at__lt=now,
    )
    expired_count = expired.update(
        state=PreemptionState.CANCELLED, reason="plan expired before activation"
    )

    return {"released": released, "expired": expired_count, "checked_at": now}


def corridor_status(trip) -> list[dict]:
    """Current corridor state for a trip - what the ops dashboard renders."""
    preemptions = (
        SignalPreemption.objects.filter(trip=trip)
        .select_related("signal", "signal__intersection")
        .order_by("planned_green_at")
    )
    return [
        {
            "id": p.id,
            "signal_id": p.signal_id,
            "controller_id": p.signal.controller_id,
            "intersection": p.signal.intersection.label,
            "latitude": p.signal.intersection.latitude,
            "longitude": p.signal.intersection.longitude,
            "state": p.state,
            "planned_green_at": p.planned_green_at,
            "planned_release_at": p.planned_release_at,
            "activated_at": p.activated_at,
            "released_at": p.released_at,
            "predicted_arrival_at": p.predicted_arrival_at,
            "actual_arrival_at": p.actual_arrival_at,
            "eta_error_s": (round(p.eta_error_s, 1) if p.eta_error_s is not None else None),
            "hold_duration_s": round(p.hold_duration_s, 1),
            "reason": p.reason,
        }
        for p in preemptions
    ]
