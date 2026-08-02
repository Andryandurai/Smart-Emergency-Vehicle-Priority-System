"""Push subscriptions, notification history and delivery receipts.

Why any of this is persisted
----------------------------
Until now a notification was a WebSocket message and nothing else. That is
fine while a dashboard is open and catastrophic when it is not: a hospital that
was on a tea break at 02:14 has no record that an inbound Level 1 was announced,
and nobody can answer "was the hospital told?" afterwards.

So three things are stored, each for a distinct reason:

* :class:`PushSubscription` - *where* a person can be reached when no socket is
  open. One row per browser/device, not per user; a controller with a desk
  machine and a phone has two.
* :class:`NotificationRecord` - *what* was said. Survives the socket, so a
  dashboard opened later still shows the last hour, and an incident review can
  reconstruct who was told what and when.
* :class:`NotificationDelivery` - *whether it arrived*. An emergency
  notification that silently failed is worse than one never sent, because
  everyone believes the message got through.

Clinical content is deliberately absent from all three. A push payload travels
through a third-party push service (Mozilla's, Google's, Apple's) and is
readable by whoever holds the device. Push says "inbound Level 1 to Apollo,
open the console"; the console, behind authentication, says the rest.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel, UUIDModel


class PushBackend(models.TextChoices):
    """How a subscription is reached.

    Web Push is the default and the one SEVPS depends on. FCM exists for native
    Android clients that hold a registration token rather than a W3C
    subscription; see ``docs/NOTIFICATIONS.md`` for why it is not the primary.
    """

    WEBPUSH = "webpush", "Web Push (VAPID)"
    FCM = "fcm", "Firebase Cloud Messaging"


class NotificationCategory(models.TextChoices):
    """What a notification is *about*, which is what people mute by.

    Severity says how loud; category says what subject. A hospital wants
    inbound-patient alerts and not signal-preemption failures, and that is a
    category choice, not a severity one.
    """

    INBOUND_PATIENT = "inbound_patient", "Inbound patient"
    CORRIDOR = "corridor", "Green corridor / signal preemption"
    PRIORITY = "priority", "Priority escalation"
    ROUTE = "route", "Route change"
    DISPATCH = "dispatch", "Dispatch and assignment"
    ROAD_HAZARD = "road_hazard", "Road hazard and closure"
    FLEET = "fleet", "Fleet health"
    SYSTEM = "system", "Platform and system"


class DeliveryState(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Accepted by push service"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Subscription gone"
    SKIPPED = "skipped", "Suppressed by preference"


class PushSubscriptionQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def for_roles(self, roles):
        """Subscriptions belonging to users holding any of ``roles``.

        Resolved through Django groups rather than a denormalised column, so a
        role granted in the admin at 09:00 takes effect on the 09:01
        notification without a resync step.
        """
        from apps.core.roles import group_names_for

        if not roles:
            return self.none()
        names: set[str] = set()
        for role in roles:
            names.update(group_names_for(role))
        return self.filter(
            models.Q(user__groups__name__in=names) | models.Q(user__is_superuser=True)
        ).distinct()

    def in_cells(self, cells):
        """Anonymous driver subscriptions inside the given geohash cells."""
        if not cells:
            return self.none()
        return self.filter(user__isnull=True, geohash__in=list(cells))


class PushSubscription(TimeStampedModel):
    """One browser or device that has granted notification permission.

    ``endpoint`` is the identity. The W3C spec makes it globally unique and it
    is what the push service routes on, so re-subscribing the same browser
    updates the existing row instead of accumulating duplicates that all
    deliver to the same place.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="push_subscriptions",
        help_text="Null for anonymous road-user devices subscribed to driver alerts.",
    )
    backend = models.CharField(
        max_length=16, choices=PushBackend.choices, default=PushBackend.WEBPUSH
    )
    endpoint = models.TextField(unique=True)
    #: W3C keys. Empty for FCM, which authenticates with a server key instead.
    p256dh = models.CharField(max_length=255, blank=True)
    auth = models.CharField(max_length=255, blank=True)

    #: Ties an anonymous subscription to its DriverDevice, so a road user's
    #: alerts and push registration are the same subject.
    device = models.ForeignKey(
        "alerts.DriverDevice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="push_subscriptions",
    )
    #: Coarse cell for driver-alert targeting. Only ~1.2 km precision is kept:
    #: delivering a warning needs a neighbourhood, not a location history.
    geohash = models.CharField(max_length=12, blank=True, db_index=True)

    user_agent = models.CharField(max_length=300, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    #: Consecutive failures. A push service that has been refusing this
    #: endpoint all day is not going to start working; see PRUNE_AFTER_FAILURES.
    failure_count = models.PositiveIntegerField(default=0)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_reason = models.CharField(max_length=200, blank=True)

    objects = PushSubscriptionQuerySet.as_manager()

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["is_active", "backend"]),
            models.Index(fields=["user", "is_active"]),
        ]

    def __str__(self) -> str:
        who = self.user.username if self.user_id else f"device {self.geohash or '?'}"
        return f"{who} via {self.backend}"

    @property
    def is_anonymous_device(self) -> bool:
        return self.user_id is None

    def record_success(self) -> None:
        self.failure_count = 0
        self.last_success_at = timezone.now()
        self.last_failure_reason = ""
        self.save(
            update_fields=["failure_count", "last_success_at", "last_failure_reason", "updated_at"]
        )

    def record_failure(self, reason: str, *, gone: bool = False) -> None:
        """``gone`` is the 404/410 case: the push service says this endpoint is
        dead. That is authoritative, so deactivate at once rather than retrying
        a subscription the browser has already discarded."""
        self.failure_count += 1
        self.last_failure_reason = reason[:200]
        if gone or self.failure_count >= PRUNE_AFTER_FAILURES:
            self.is_active = False
        self.save(
            update_fields=["failure_count", "last_failure_reason", "is_active", "updated_at"]
        )


#: Consecutive failures tolerated before a subscription is retired. Low,
#: because every failed attempt costs a blocking HTTPS request in the fan-out.
PRUNE_AFTER_FAILURES = 5


class NotificationPreference(TimeStampedModel):
    """Per-user, per-category mute.

    Deliberately a *mute list* rather than a subscribe list: a new category
    added by a later release reaches everyone by default. The alternative
    fails silent, which for operational alerting is the wrong direction.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference"
    )
    muted_categories = models.JSONField(default=list, blank=True)
    #: Suppress non-critical push (not in-app) during these hours, local time.
    quiet_hours_start = models.PositiveSmallIntegerField(null=True, blank=True)
    quiet_hours_end = models.PositiveSmallIntegerField(null=True, blank=True)
    push_enabled = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"Preferences for {self.user}"

    def allows(self, category: str, severity: str) -> bool:
        """Critical notifications ignore every preference.

        A hospital cannot mute "inbound Level 1", and a controller cannot mute
        "corridor failed". Muting exists so the important messages stay
        visible, not so they can be turned off.
        """
        from apps.core.notifications import Severity

        if severity == Severity.CRITICAL:
            return True
        if not self.push_enabled:
            return False
        if category in (self.muted_categories or []):
            return False
        return not self.in_quiet_hours()

    def in_quiet_hours(self, now=None) -> bool:
        if self.quiet_hours_start is None or self.quiet_hours_end is None:
            return False
        hour = (now or timezone.localtime()).hour
        start, end = self.quiet_hours_start, self.quiet_hours_end
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end  # window crosses midnight


class NotificationRecord(TimeStampedModel, UUIDModel):
    """A notification as issued - the durable copy behind the socket event."""

    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    severity = models.CharField(max_length=16, default="info", db_index=True)
    category = models.CharField(
        max_length=32, choices=NotificationCategory.choices,
        default=NotificationCategory.SYSTEM, db_index=True,
    )
    audience = models.JSONField(default=list, blank=True)
    link = models.CharField(max_length=300, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True, db_index=True)
    context = models.JSONField(default=dict, blank=True)
    trip = models.ForeignKey(
        "dispatch.EmergencyTrip", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="notifications",
    )
    #: Rolled up after fan-out so an operator can see reach without a join.
    delivered_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    read_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="read_notifications"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at", "severity"]),
            models.Index(fields=["dedupe_key", "-created_at"]),
            models.Index(fields=["category", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.title}"

    def as_payload(self) -> dict:
        return {
            "id": str(self.uuid),
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "category": self.category,
            "audience": self.audience,
            "link": self.link,
            "dedupe_key": self.dedupe_key,
            "issued_at": self.created_at.isoformat(),
            "context": self.context,
        }


class NotificationDelivery(TimeStampedModel):
    """One attempt to reach one subscription. The audit trail for "was it told?"."""

    notification = models.ForeignKey(
        NotificationRecord, on_delete=models.CASCADE, related_name="deliveries"
    )
    subscription = models.ForeignKey(
        PushSubscription, on_delete=models.CASCADE, related_name="deliveries"
    )
    state = models.CharField(
        max_length=12, choices=DeliveryState.choices, default=DeliveryState.PENDING, db_index=True
    )
    backend = models.CharField(max_length=16, choices=PushBackend.choices)
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    detail = models.CharField(max_length=250, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["notification", "state"])]

    def __str__(self) -> str:
        return f"{self.notification_id} -> {self.subscription_id}: {self.state}"
