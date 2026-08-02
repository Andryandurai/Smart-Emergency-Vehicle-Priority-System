"""Serializers for subscription registration, preferences and the inbox."""
from __future__ import annotations

from rest_framework import serializers

from apps.notify.models import (
    NotificationCategory,
    NotificationPreference,
    NotificationRecord,
    PushBackend,
    PushSubscription,
)


class WebPushKeysSerializer(serializers.Serializer):
    p256dh = serializers.CharField(max_length=255)
    auth = serializers.CharField(max_length=255)


class SubscribeSerializer(serializers.Serializer):
    """Accepts a `PushSubscription.toJSON()` straight from the browser.

    Shaped to the W3C object rather than a SEVPS-specific body so the client
    can forward what the Push API handed it without reshaping — one fewer
    place for a transcription bug between the browser and the endpoint that
    has to match it exactly.
    """

    endpoint = serializers.CharField(max_length=2000)
    keys = WebPushKeysSerializer(required=False)
    backend = serializers.ChoiceField(choices=PushBackend.choices, default=PushBackend.WEBPUSH)
    #: Anonymous road-user registration only.
    device_id = serializers.CharField(max_length=128, required=False, allow_blank=True)
    latitude = serializers.FloatField(required=False)
    longitude = serializers.FloatField(required=False)

    def validate(self, attrs):
        backend = attrs.get("backend", PushBackend.WEBPUSH)
        if backend == PushBackend.WEBPUSH and not attrs.get("keys"):
            # Without p256dh/auth the payload cannot be encrypted, so every
            # push to this endpoint would fail. Reject now, loudly, rather than
            # storing a subscription that can never deliver.
            raise serializers.ValidationError(
                {"keys": "Web Push requires the p256dh and auth keys from the browser."}
            )
        if (attrs.get("latitude") is None) != (attrs.get("longitude") is None):
            raise serializers.ValidationError("Provide both latitude and longitude, or neither.")
        return attrs


class UnsubscribeSerializer(serializers.Serializer):
    endpoint = serializers.CharField(max_length=2000)


class PushSubscriptionSerializer(serializers.ModelSerializer):
    """Read view of a user's own devices.

    ``endpoint`` is truncated: it is a bearer capability — anyone holding it
    can send to that browser — so the full value never leaves the server after
    registration. Enough is shown to tell two devices apart.
    """

    endpoint_hint = serializers.SerializerMethodField()
    is_healthy = serializers.SerializerMethodField()

    class Meta:
        model = PushSubscription
        fields = [
            "id", "backend", "endpoint_hint", "user_agent", "is_active",
            "failure_count", "is_healthy", "last_success_at",
            "last_failure_reason", "created_at",
        ]

    def get_endpoint_hint(self, obj) -> str:
        return f"...{obj.endpoint[-12:]}" if obj.endpoint else ""

    def get_is_healthy(self, obj) -> bool:
        return obj.is_active and obj.failure_count == 0


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    available_categories = serializers.SerializerMethodField()

    class Meta:
        model = NotificationPreference
        fields = [
            "muted_categories", "quiet_hours_start", "quiet_hours_end",
            "push_enabled", "available_categories",
        ]

    def get_available_categories(self, _obj) -> list[dict]:
        return [{"value": v, "label": l} for v, l in NotificationCategory.choices]

    def validate_muted_categories(self, value):
        valid = {v for v, _ in NotificationCategory.choices}
        unknown = set(value) - valid
        if unknown:
            raise serializers.ValidationError(f"Unknown categories: {sorted(unknown)}")
        return value

    def validate(self, attrs):
        for field in ("quiet_hours_start", "quiet_hours_end"):
            hour = attrs.get(field)
            if hour is not None and not 0 <= hour <= 23:
                raise serializers.ValidationError({field: "Must be an hour, 0-23."})
        return attrs


class NotificationRecordSerializer(serializers.ModelSerializer):
    is_read = serializers.SerializerMethodField()
    severity_label = serializers.SerializerMethodField()
    category_label = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = NotificationRecord
        fields = [
            "uuid", "title", "body", "severity", "severity_label", "category",
            "category_label", "link", "dedupe_key", "context", "created_at",
            "delivered_count", "failed_count", "is_read",
        ]

    def get_is_read(self, obj) -> bool:
        user = self.context.get("user")
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        # Prefetched by the view; falling back to a query here would make the
        # inbox N+1 on a list that is read constantly.
        read_ids = self.context.get("read_ids")
        if read_ids is not None:
            return obj.id in read_ids
        return obj.read_by.filter(pk=user.pk).exists()

    def get_severity_label(self, obj) -> str:
        return {"info": "Info", "success": "Resolved",
                "warning": "Attention", "critical": "Critical"}.get(obj.severity, obj.severity)
