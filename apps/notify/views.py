"""Push registration, preferences and the notification inbox."""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import (
    IsAuthenticatedRole,
    PublicDeviceRegistration,
    PublicRead,
)
from apps.notify import vapid
from apps.notify.models import (
    NotificationPreference,
    NotificationRecord,
    PushBackend,
    PushSubscription,
)
from apps.notify.serializers import (
    NotificationPreferenceSerializer,
    NotificationRecordSerializer,
    PushSubscriptionSerializer,
    SubscribeSerializer,
    UnsubscribeSerializer,
)
from apps.notify.service import deliver, record_notification

log = logging.getLogger("sevps.notify")

#: How much history the inbox returns. A notification centre is for "what did I
#: miss", not for archival search; the full record stays queryable in admin.
INBOX_LIMIT = 50
INBOX_WINDOW_HOURS = 24


class VapidPublicKeyView(APIView):
    """The key a browser needs before it can subscribe.

    Public by necessity — it is handed to every client that subscribes, and the
    Push API requires it before the permission prompt can even be shown. It is
    a public key; it authenticates the *server* to the push service and grants
    nothing.
    """

    permission_classes = [PublicRead]

    def get(self, request):
        key = vapid.public_key()
        return Response(
            {
                "public_key": key,
                "configured": bool(key),
                "subject": ("configured" if key else ""),
                "hint": "" if key else "Run `python manage.py generate_vapid_keys` on the server.",
            }
        )


class SubscribeView(APIView):
    """Register a browser or device for push.

    Open to anonymous callers because Layer 4 requires it: a road user's phone
    subscribes to approaching-ambulance warnings without an account, exactly as
    ``alerts-nearby`` already allows. An anonymous subscription is stored with
    no user, is targeted only by geohash cell, and only ever receives the
    anonymous driver-alert payload.
    """

    permission_classes = [PublicDeviceRegistration]

    def post(self, request):
        serializer = SubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user if request.user.is_authenticated else None
        keys = data.get("keys") or {}
        device, cell = self._resolve_device(data, user)

        defaults = {
            "user": user,
            "backend": data["backend"],
            "p256dh": keys.get("p256dh", ""),
            "auth": keys.get("auth", ""),
            "device": device,
            "geohash": cell,
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300],
            "is_active": True,
            "failure_count": 0,
            "last_failure_reason": "",
        }

        # The endpoint is the browser's identity for this subscription, so
        # re-subscribing updates rather than duplicates. Without this a user
        # who reloads with permission already granted collects a new row each
        # time and receives one notification per reload.
        try:
            with transaction.atomic():
                subscription, created = PushSubscription.objects.update_or_create(
                    endpoint=data["endpoint"], defaults=defaults
                )
        except IntegrityError:  # pragma: no cover - concurrent identical subscribe
            subscription = PushSubscription.objects.get(endpoint=data["endpoint"])
            created = False

        return Response(
            {
                "created": created,
                "subscription": PushSubscriptionSerializer(subscription).data,
                "push_configured": vapid.is_configured()
                or data["backend"] == PushBackend.FCM,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def _resolve_device(self, data, user):
        """Link an anonymous subscription to its DriverDevice and cell."""
        from apps.alerts.models import DriverDevice
        from apps.core.geo import geohash as encode

        device_id = data.get("device_id", "")
        latitude, longitude = data.get("latitude"), data.get("longitude")
        cell = encode(latitude, longitude, 6) if latitude is not None else ""

        if user is not None or not device_id:
            return None, cell

        device, _ = DriverDevice.objects.get_or_create(
            device_id=device_id,
            defaults={"latitude": latitude or 0.0, "longitude": longitude or 0.0},
        )
        if latitude is not None:
            device.update_position(latitude, longitude)
            cell = device.geohash
        return device, cell


class UnsubscribeView(APIView):
    """Retire a subscription.

    Public for the same reason as subscribe, and safe: the endpoint is the
    secret. Someone who can present it already holds the capability to send to
    that browser, so being able to switch it off adds no exposure.
    """

    permission_classes = [PublicDeviceRegistration]

    def post(self, request):
        serializer = UnsubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = PushSubscription.objects.filter(
            endpoint=serializer.validated_data["endpoint"]
        ).update(is_active=False, last_failure_reason="unsubscribed", updated_at=timezone.now())
        return Response({"removed": bool(updated)})


class MySubscriptionsView(APIView):
    """The signed-in user's own devices — and only their own."""

    permission_classes = [IsAuthenticatedRole]

    def get(self, request):
        subscriptions = PushSubscription.objects.filter(user=request.user).order_by("-updated_at")
        return Response(
            {
                "subscriptions": PushSubscriptionSerializer(subscriptions, many=True).data,
                "push_configured": vapid.is_configured(),
            }
        )


class PreferenceView(APIView):
    """Read and update notification preferences.

    Critical notifications ignore all of this; see
    :meth:`NotificationPreference.allows`. The response says so explicitly so
    the UI can show it rather than implying a mute that will not happen.
    """

    permission_classes = [IsAuthenticatedRole]

    def get(self, request):
        preference, _ = NotificationPreference.objects.get_or_create(user=request.user)
        return Response(self._body(preference))

    def patch(self, request):
        preference, _ = NotificationPreference.objects.get_or_create(user=request.user)
        serializer = NotificationPreferenceSerializer(preference, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self._body(preference))

    def _body(self, preference) -> dict:
        data = NotificationPreferenceSerializer(preference).data
        data["critical_always_delivered"] = True
        data["note"] = (
            "Critical alerts (inbound Level 1, corridor failure, priority escalation) "
            "are delivered regardless of these settings."
        )
        return data


class InboxView(APIView):
    """Recent notifications for this user's roles — the durable side of the socket.

    Answers "what did I miss while this tab was closed", which the WebSocket
    stream structurally cannot: it only carries what happened while connected.
    """

    permission_classes = [IsAuthenticatedRole]

    def get(self, request):
        from apps.core.roles import Role, user_roles

        roles = user_roles(request.user)
        since = timezone.now() - timezone.timedelta(hours=INBOX_WINDOW_HOURS)

        queryset = NotificationRecord.objects.filter(created_at__gte=since)
        if Role.ADMIN not in roles:
            # An empty audience means "everyone operational", so those match
            # any role; a populated audience must intersect the user's roles.
            queryset = [
                record for record in queryset[: INBOX_LIMIT * 4]
                if not record.audience or set(record.audience) & roles
            ][:INBOX_LIMIT]
        else:
            queryset = list(queryset[:INBOX_LIMIT])

        read_ids = set(
            NotificationRecord.objects.filter(
                read_by=request.user, pk__in=[r.pk for r in queryset]
            ).values_list("id", flat=True)
        )
        serializer = NotificationRecordSerializer(
            queryset, many=True, context={"user": request.user, "read_ids": read_ids}
        )
        return Response(
            {
                "notifications": serializer.data,
                "unread": sum(1 for record in queryset if record.id not in read_ids),
                "window_hours": INBOX_WINDOW_HOURS,
            }
        )


@api_view(["POST"])
@permission_classes([IsAuthenticatedRole])
def mark_read(request, uuid=None):
    """Mark one notification, or everything in the window, as read."""
    if uuid:
        try:
            record = NotificationRecord.objects.get(uuid=uuid)
        except (NotificationRecord.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Notification not found."}, status=404)
        record.read_by.add(request.user)
        return Response({"read": 1})

    since = timezone.now() - timezone.timedelta(hours=INBOX_WINDOW_HOURS)
    records = NotificationRecord.objects.filter(created_at__gte=since).exclude(
        read_by=request.user
    )
    count = 0
    for record in records[:200]:
        record.read_by.add(request.user)
        count += 1
    return Response({"read": count})


@api_view(["POST"])
@permission_classes([IsAuthenticatedRole])
def send_test(request):
    """Send a test push to the caller's own devices.

    Present because push has an unusually long failure chain — permission,
    service worker, VAPID key, subscription, push service, encryption — and a
    controller who wants to know their alerts will arrive should not have to
    wait for a real emergency to find out.

    Deliberately restricted to the caller's own subscriptions, so it cannot be
    used to notify anyone else.
    """
    subscriptions = list(PushSubscription.objects.active().filter(user=request.user))
    if not subscriptions:
        return Response(
            {"detail": "No active push subscriptions for this account.",
             "delivered": 0, "attempted": 0},
            status=status.HTTP_409_CONFLICT,
        )

    record = record_notification(
        {
            "title": "SEVPS test notification",
            "body": f"Push is working for {request.user.username}.",
            "severity": "info",
            "audience": [],
            "dedupe_key": f"test:{request.user.pk}",
            "context": {"test": True},
        },
        category="system",
    )
    result = deliver(record, subscriptions=subscriptions)
    return Response(result.as_dict())


class PushHealthView(APIView):
    """Whether push can actually deliver right now.

    Separate from ``/api/v1/health/`` because the answer is different in kind:
    the platform can be perfectly healthy while push has been silently
    undeliverable since the day a key was not generated. Reporting zero
    configured keys as "healthy" is exactly the failure this endpoint exists to
    prevent.
    """

    permission_classes = [IsAuthenticatedRole]

    def get(self, request):
        from django.conf import settings

        active = PushSubscription.objects.active()
        since = timezone.now() - timezone.timedelta(hours=24)
        recent = NotificationRecord.objects.filter(created_at__gte=since)
        attempted = sum(r.delivered_count + r.failed_count for r in recent)
        delivered = sum(r.delivered_count for r in recent)

        configured = vapid.is_configured()
        return Response(
            {
                "webpush_configured": configured,
                "fcm_configured": bool(settings.SEVPS.get("FCM_CREDENTIALS", "")),
                "active_subscriptions": active.count(),
                "authenticated_subscriptions": active.filter(user__isnull=False).count(),
                "anonymous_device_subscriptions": active.filter(user__isnull=True).count(),
                "retired_subscriptions": PushSubscription.objects.filter(is_active=False).count(),
                "notifications_24h": recent.count(),
                "delivery_attempts_24h": attempted,
                "delivered_24h": delivered,
                "delivery_rate": round(delivered / attempted, 3) if attempted else None,
                "status": "ok" if configured else "push not configured",
                "hint": "" if configured else "python manage.py generate_vapid_keys",
            }
        )
