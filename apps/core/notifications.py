"""Unified notification stream.

Before this, "something happened" was expressed a dozen different ways: an
``inbound_update`` here, a ``signal_preempted`` there, each with its own shape.
A client that wanted to show a notification banner had to know all of them.

A notification is a *deliberate message to a person*, distinct from a state
update. Two different things were being conflated:

* **state events** - the map moved, the ETA changed. Frequent, disposable,
  and only meaningful to a screen that is already rendering that state.
* **notifications** - a hospital must prepare a bay; a controller must know a
  corridor failed. Infrequent, individually important, and worth surfacing
  even to a screen showing something else.

Only the second kind belongs here. Routing is by *role and interest* rather
than by socket, so the same notification reaches whoever needs it without the
publisher knowing which dashboards are open.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from django.utils import timezone

from apps.core.realtime import broadcast, broadcast_ops, hospital_group, vehicle_group
from apps.core.roles import Role

log = logging.getLogger("sevps.notifications")

EVENT = "notification"


class Severity:
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Notification:
    """One message for a person, not a state delta for a screen."""

    title: str
    body: str = ""
    severity: str = Severity.INFO
    #: Roles this matters to. Empty means everyone with an operational socket.
    audience: tuple[str, ...] = ()
    #: Where a client should navigate when the notification is acted on.
    link: str = ""
    #: Groups the message is fanned out to beyond `ops`.
    extra_groups: tuple[str, ...] = field(default_factory=tuple)
    #: Correlates related notifications so a client can supersede rather than
    #: stack them - three reroutes of one trip is one story, not three.
    dedupe_key: str = ""
    context: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "audience": list(self.audience),
            "link": self.link,
            "dedupe_key": self.dedupe_key,
            "issued_at": timezone.now().isoformat(),
            "context": self.context,
        }


def publish(notification: Notification) -> dict:
    """Fan a notification out to the ops stream, extra groups, and push.

    Two delivery paths with different jobs. The WebSocket reaches dashboards
    that are *open right now*, instantly. Push (Phase 9) reaches the people who
    are not looking at a screen — which, at 02:14, is all of them — and leaves
    a durable record so the notification survives the socket.

    Push is best-effort and additive: :func:`apps.notify.service.publish_and_deliver`
    swallows its own failures, and the import is local so a deployment without
    the notify app still publishes normally.
    """
    payload = notification.as_dict()
    broadcast_ops(EVENT, payload)
    for group in notification.extra_groups:
        broadcast(group, EVENT, payload)
    log.info("notification: [%s] %s", notification.severity, notification.title)

    try:
        from apps.notify.service import publish_and_deliver

        result = publish_and_deliver(payload)
        if result.record is not None:
            # Replace the throwaway UUID with the stored one, so a client can
            # mark this exact notification read through the REST API.
            payload["id"] = str(result.record.uuid)
            payload["category"] = result.record.category
    except Exception:  # pragma: no cover - fan-out must never break dispatch
        log.warning("push fan-out unavailable for '%s'", notification.title, exc_info=True)

    return payload


# ---------------------------------------------------------------------------
# The notifications SEVPS actually raises.
#
# Defined as functions rather than constructed inline at each call site, so
# the wording and audience of an operationally important message is decided
# once and is reviewable in one place.
# ---------------------------------------------------------------------------
def hospital_prepare(trip, hospital) -> dict:
    return publish(
        Notification(
            title=f"Inbound {trip.get_emergency_category_display()}",
            body=(
                f"{trip.vehicle.callsign} is en route to {hospital.name}. "
                f"Priority level {trip.priority_level}."
            ),
            severity=Severity.CRITICAL if trip.priority_level == 1 else Severity.WARNING,
            audience=(Role.HOSPITAL, Role.ADMIN),
            link=f"/hospital/{hospital.code}",
            extra_groups=(hospital_group(hospital.code),),
            dedupe_key=f"inbound:{trip.id}",
            context={"trip_id": trip.id, "reference": trip.reference},
        )
    )


def corridor_failed(trip, controller_id: str, detail: str) -> dict:
    """A junction that could not be preempted.

    Traffic control needs this: the crew is about to meet a red light the
    platform promised would be green.
    """
    return publish(
        Notification(
            title=f"Signal preemption failed at {controller_id}",
            body=f"{trip.vehicle.callsign}: {detail}. Junction stays on normal timing.",
            severity=Severity.WARNING,
            audience=(Role.ADMIN,),
            extra_groups=(vehicle_group(trip.vehicle.callsign),),
            dedupe_key=f"corridor-fail:{trip.id}:{controller_id}",
            context={"trip_id": trip.id, "controller_id": controller_id},
        )
    )


def priority_escalated(trip, previous_level: int, trigger: str) -> dict:
    return publish(
        Notification(
            title=f"{trip.reference} escalated to Level {trip.priority_level}",
            body=f"{trigger}. Was Level {previous_level}.",
            severity=Severity.CRITICAL,
            audience=(Role.ADMIN, Role.HOSPITAL),
            extra_groups=(vehicle_group(trip.vehicle.callsign),),
            dedupe_key=f"escalation:{trip.id}",
            context={"trip_id": trip.id, "priority_level": trip.priority_level},
        )
    )


def route_blocked(trip, reason: str) -> dict:
    return publish(
        Notification(
            title=f"{trip.reference} rerouted",
            body=reason,
            severity=Severity.WARNING,
            audience=(Role.ADMIN,),
            extra_groups=(vehicle_group(trip.vehicle.callsign),),
            dedupe_key=f"reroute:{trip.id}",
            context={"trip_id": trip.id},
        )
    )


def no_hospital_available(trip, detail: str) -> dict:
    """The one case the recommender escalates to a human."""
    return publish(
        Notification(
            title=f"No receiving hospital for {trip.reference}",
            body=detail,
            severity=Severity.CRITICAL,
            audience=(Role.ADMIN,),
            dedupe_key=f"no-hospital:{trip.id}",
            context={"trip_id": trip.id},
        )
    )


# ---------------------------------------------------------------------------
# Driver module (Phase 13): readiness, maintenance and in-transport breakdown
# ---------------------------------------------------------------------------
def inspection_skipped(vehicle, shift, reason: str) -> dict:
    """A vehicle went out without its check.

    Deliberately a notification and not merely a log line. The skip is the
    right call in an emergency, but somebody has to be told that a vehicle is
    running on an unverified inspection, and has to be able to chase it once
    the call is over.
    """
    return publish(
        Notification(
            title=f"{vehicle.callsign} dispatched without inspection",
            body=(
                f"Driver skipped the readiness check: {reason}. "
                f"Vehicle is temporarily ready - inspection still outstanding."
            ),
            severity=Severity.WARNING,
            audience=(Role.ADMIN,),
            link="/fleet",
            dedupe_key=f"inspection-skipped:{shift.id}",
            context={"vehicle": vehicle.callsign, "shift_id": shift.id},
        )
    )


def vehicle_not_ready(vehicle, report) -> dict:
    """A failed inspection has grounded a vehicle."""
    return publish(
        Notification(
            title=f"{vehicle.callsign} failed readiness check",
            body=(
                f"Faults: {', '.join(report.reasons) or 'unspecified'}. "
                f"Vehicle is grounded until fleet management clears it."
            ),
            severity=Severity.CRITICAL,
            audience=(Role.ADMIN,),
            link="/fleet",
            dedupe_key=f"not-ready:{vehicle.id}",
            context={"vehicle": vehicle.callsign, "report_id": report.id},
        )
    )


def ambulance_breakdown(breakdown) -> dict:
    """An ambulance has failed with a patient on board.

    The widest audience of any notification in the system, and correctly so:
    dispatch has to find a replacement, the hospital has to expect a delay or
    a different vehicle, and the control room has to know a corridor is about
    to be abandoned mid-route.
    """
    trip = breakdown.trip
    hospital = trip.destination_hospital
    extra = (hospital_group(hospital.code),) if hospital else ()
    return publish(
        Notification(
            title=f"BREAKDOWN - {breakdown.vehicle.callsign} with patient on board",
            body=(
                f"{trip.reference} - {trip.get_emergency_category_display()}, "
                f"priority {trip.priority_level}"
                + (f", bound for {hospital.name}" if hospital else "")
                + ". Seeking replacement ambulance."
            ),
            severity=Severity.CRITICAL,
            audience=(Role.ADMIN, Role.HOSPITAL),
            link="/fleet",
            extra_groups=extra,
            dedupe_key=f"breakdown:{breakdown.id}",
            context={"breakdown_id": breakdown.id, "trip_id": trip.id},
        )
    )


def transfer_accepted(breakdown, vehicle) -> dict:
    trip = breakdown.trip
    hospital = trip.destination_hospital
    extra = (hospital_group(hospital.code),) if hospital else ()
    return publish(
        Notification(
            title=f"{vehicle.callsign} taking over {trip.reference}",
            body=(
                f"Replacement accepted for {breakdown.vehicle.callsign}. "
                f"Patient transfer in progress."
            ),
            severity=Severity.WARNING,
            audience=(Role.ADMIN, Role.HOSPITAL),
            extra_groups=extra + (vehicle_group(vehicle.callsign),),
            dedupe_key=f"breakdown:{breakdown.id}",
            context={"breakdown_id": breakdown.id, "trip_id": trip.id},
        )
    )
