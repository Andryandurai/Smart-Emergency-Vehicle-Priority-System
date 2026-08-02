"""Turning a notification into deliveries.

The pipeline, in order:

    Notification -> persist record -> resolve audience -> apply preferences
                 -> fan out per backend -> record receipts

Audience resolution is by **role**, never by user list. ``hospital_prepare``
says "hospital staff and dispatchers need this"; who currently holds those
roles is a question for the moment of sending, so a nurse added to the group
this morning is reached this afternoon with no resync.

Delivery is synchronous but strictly bounded. Doing it inline keeps the causal
chain intact — the same request that assigned a hospital either tells the
hospital or records why it could not — and SEVPS has no broker in the stack
yet. The bound matters: ``MAX_FANOUT`` caps recipients per notification and the
HTTP timeout is 6 s, so the worst case is bounded rather than "as long as the
slowest push service takes". Once Celery or a task queue arrives, only
:func:`deliver` moves.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from apps.core.notifications import Severity
from apps.notify.backends import get_backend
from apps.notify.backends.webpush import CRITICAL_TTL_S, DEFAULT_TTL_S
from apps.notify.models import (
    DeliveryState,
    NotificationCategory,
    NotificationDelivery,
    NotificationPreference,
    NotificationRecord,
    PushSubscription,
)

log = logging.getLogger("sevps.notify")

#: Hard ceiling on recipients for one notification. A city-wide road-hazard
#: alert could otherwise address every registered driver device inline and turn
#: one API call into ten thousand HTTPS requests. Anything above this is a
#: broadcast and needs a queue, so it is capped and logged rather than
#: attempted and silently truncated.
MAX_FANOUT = 500

#: RFC 8030 urgency. Push services may hold "normal" messages to save battery;
#: a critical alert should wake the device.
URGENCY = {
    Severity.CRITICAL: "high",
    Severity.WARNING: "high",
    Severity.SUCCESS: "normal",
    Severity.INFO: "normal",
}


@dataclass
class FanoutResult:
    record: NotificationRecord | None
    attempted: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    truncated: bool = False
    backend_note: str = ""

    def as_dict(self) -> dict:
        return {
            "notification_id": str(self.record.uuid) if self.record else None,
            "attempted": self.attempted,
            "delivered": self.delivered,
            "failed": self.failed,
            "skipped": self.skipped,
            "truncated": self.truncated,
            "backend_note": self.backend_note,
        }


def record_notification(payload: dict, *, category: str = "", trip=None) -> NotificationRecord:
    """Persist the durable copy. Independent of push, on purpose.

    Even with no push transport configured the history has to exist: the
    in-app notification centre reads it, and an incident review reads it.
    """
    context = payload.get("context") or {}
    trip_id = trip.id if trip is not None else context.get("trip_id")
    return NotificationRecord.objects.create(
        title=payload.get("title", "")[:200],
        body=payload.get("body", ""),
        severity=payload.get("severity", Severity.INFO),
        category=category or infer_category(payload),
        audience=list(payload.get("audience") or []),
        link=payload.get("link", ""),
        dedupe_key=payload.get("dedupe_key", "")[:120],
        context=context,
        trip_id=trip_id,
    )


#: Substrings that identify what a notification is about. Matched against the
#: dedupe_key, which the notification helpers already set to a stable prefix -
#: reusing it avoids asking every existing call site to declare a category.
_CATEGORY_HINTS = (
    ("inbound", NotificationCategory.INBOUND_PATIENT),
    ("corridor", NotificationCategory.CORRIDOR),
    ("escalation", NotificationCategory.PRIORITY),
    ("reroute", NotificationCategory.ROUTE),
    ("no-hospital", NotificationCategory.DISPATCH),
    ("hazard", NotificationCategory.ROAD_HAZARD),
    ("closure", NotificationCategory.ROAD_HAZARD),
    ("fleet", NotificationCategory.FLEET),
)


def infer_category(payload: dict) -> str:
    key = (payload.get("dedupe_key") or "").lower()
    for hint, category in _CATEGORY_HINTS:
        if hint in key:
            return category
    return NotificationCategory.SYSTEM


def resolve_audience(payload: dict) -> list[PushSubscription]:
    """Which subscriptions this notification is for.

    An empty audience means "every signed-in operational role" - the same
    meaning it already has for the WebSocket fan-out in ``core.notifications``.
    It deliberately does *not* mean anonymous driver devices: those are
    targeted geographically by the driver-alert path, and a road user should
    not receive "corridor preemption failed at TSC-114".
    """
    from apps.core.roles import ALL_ROLES, Role

    roles = payload.get("audience") or [r for r in ALL_ROLES if r != Role.PUBLIC]
    queryset = (
        PushSubscription.objects.active()
        .filter(user__isnull=False)
        .for_roles(roles)
        .select_related("user")
    )
    return list(queryset[: MAX_FANOUT + 1])


def resolve_driver_audience(cells) -> list[PushSubscription]:
    """Anonymous road-user devices in the given geohash cells (Layer 4)."""
    return list(
        PushSubscription.objects.active().in_cells(cells)[: MAX_FANOUT + 1]
    )


def _preference_for(user) -> NotificationPreference | None:
    try:
        return user.notification_preference
    except NotificationPreference.DoesNotExist:
        return None


def push_payload(record: NotificationRecord) -> dict:
    """What actually crosses the wire.

    Trimmed on purpose. A push payload passes through a third-party relay and
    is readable by anyone holding the unlocked device, so it carries enough to
    decide whether to act — who, how urgent, where to look — and nothing
    clinical. `context` is dropped entirely: it holds trip internals.
    """
    return {
        "id": str(record.uuid),
        "title": record.title,
        "body": record.body,
        "severity": record.severity,
        "category": record.category,
        "link": record.link,
        "dedupe_key": record.dedupe_key or str(record.uuid),
        "issued_at": record.created_at.isoformat(),
    }


def deliver(record: NotificationRecord, subscriptions=None) -> FanoutResult:
    """Push one notification to its audience and record every outcome."""
    payload_for_audience = {"audience": record.audience}
    targets = subscriptions if subscriptions is not None else resolve_audience(payload_for_audience)

    result = FanoutResult(record=record)
    if len(targets) > MAX_FANOUT:
        result.truncated = True
        targets = targets[:MAX_FANOUT]
        log.warning(
            "notification %s addressed more than %d subscriptions; delivering to the "
            "first %d. A broadcast this size needs a task queue.",
            record.uuid, MAX_FANOUT, MAX_FANOUT,
        )

    body = push_payload(record)
    ttl = CRITICAL_TTL_S if record.severity == Severity.CRITICAL else DEFAULT_TTL_S
    urgency = URGENCY.get(record.severity, "normal")

    receipts: list[NotificationDelivery] = []
    backends: dict[str, object] = {}

    for subscription in targets:
        preference = _preference_for(subscription.user) if subscription.user_id else None
        if preference and not preference.allows(record.category, record.severity):
            result.skipped += 1
            receipts.append(
                NotificationDelivery(
                    notification=record, subscription=subscription,
                    backend=subscription.backend, state=DeliveryState.SKIPPED,
                    detail="muted by preference",
                )
            )
            continue

        backend = backends.get(subscription.backend)
        if backend is None:
            backend = backends[subscription.backend] = get_backend(subscription.backend)
        if getattr(backend, "name", "") == "console" and not result.backend_note:
            result.backend_note = backend.unavailable_reason()

        result.attempted += 1
        outcome = backend.send(subscription, body, ttl=ttl, urgency=urgency)

        if outcome.ok:
            result.delivered += 1
            subscription.record_success()
            state = DeliveryState.SENT
        else:
            result.failed += 1
            subscription.record_failure(outcome.detail, gone=outcome.gone)
            state = DeliveryState.EXPIRED if outcome.gone else DeliveryState.FAILED

        receipts.append(
            NotificationDelivery(
                notification=record, subscription=subscription, backend=subscription.backend,
                state=state, status_code=outcome.status_code,
                detail=outcome.detail, latency_ms=outcome.latency_ms,
            )
        )

    if receipts:
        NotificationDelivery.objects.bulk_create(receipts)

    NotificationRecord.objects.filter(pk=record.pk).update(
        delivered_count=result.delivered, failed_count=result.failed
    )
    record.delivered_count = result.delivered
    record.failed_count = result.failed

    if result.failed and not result.delivered and result.attempted:
        log.warning(
            "notification %s reached nobody: %d attempts, all failed",
            record.uuid, result.attempted,
        )
    return result


def publish_and_deliver(payload: dict, *, category: str = "", trip=None) -> FanoutResult:
    """Entry point used by ``core.notifications.publish``.

    Fail-soft to match the WebSocket fan-out it sits beside: if push breaks,
    the dispatch decision that raised the notification still stands, and the
    dashboards still got the socket event.
    """
    try:
        with transaction.atomic():
            record = record_notification(payload, category=category, trip=trip)
    except Exception:
        log.exception("could not persist notification: %s", payload.get("title"))
        return FanoutResult(record=None)

    try:
        return deliver(record)
    except Exception:
        log.exception("push fan-out failed for notification %s", record.uuid)
        return FanoutResult(record=record)


def deliver_driver_alert(alert, cells) -> FanoutResult:
    """Layer 4: warn road users in the cells ahead of an emergency vehicle.

    Separate from :func:`deliver` because the audience is geographic rather
    than role-based, and because the message must stay anonymous — a road user
    is told an ambulance is approaching and which lane to leave, never which
    trip, which hospital or which patient.
    """
    record = NotificationRecord.objects.create(
        title="Emergency vehicle approaching",
        body=f"{alert.message} {alert.instruction}.".strip(),
        severity=Severity.CRITICAL if alert.priority_level == 1 else Severity.WARNING,
        category=NotificationCategory.ROAD_HAZARD,
        audience=[],
        dedupe_key=f"driver-alert:{alert.geohash}",
        context={
            "eta_seconds": round(alert.eta_seconds),
            "approach_bearing_deg": round(alert.approach_bearing_deg, 1),
            "radius_m": alert.radius_m,
        },
    )
    return deliver(record, subscriptions=resolve_driver_audience(cells))


def prune_dead_subscriptions(older_than_days: int = 60) -> int:
    """Retire subscriptions that are inactive or have gone quiet.

    A browser that has not accepted a push in two months is a machine that was
    reimaged. Keeping the row costs a doomed HTTPS request on every fan-out.
    """
    cutoff = timezone.now() - timezone.timedelta(days=older_than_days)
    stale = PushSubscription.objects.filter(
        is_active=True, last_success_at__isnull=False, last_success_at__lt=cutoff
    )
    count = stale.update(is_active=False, last_failure_reason="inactive - pruned")
    dead = PushSubscription.objects.filter(is_active=False).count()
    log.info("pruned %d stale subscriptions (%d inactive in total)", count, dead)
    return count
