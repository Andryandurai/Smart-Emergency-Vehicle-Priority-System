from django.contrib import admin

from apps.notify.models import (
    NotificationDelivery,
    NotificationPreference,
    NotificationRecord,
    PushSubscription,
)


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("__str__", "backend", "is_active", "failure_count",
                    "last_success_at", "geohash")
    list_filter = ("backend", "is_active")
    search_fields = ("user__username", "geohash", "endpoint")
    readonly_fields = ("endpoint", "p256dh", "auth", "created_at", "updated_at")


class DeliveryInline(admin.TabularInline):
    model = NotificationDelivery
    extra = 0
    readonly_fields = ("subscription", "backend", "state", "status_code", "detail", "latency_ms")
    can_delete = False


@admin.register(NotificationRecord)
class NotificationRecordAdmin(admin.ModelAdmin):
    list_display = ("title", "severity", "category", "delivered_count",
                    "failed_count", "created_at")
    list_filter = ("severity", "category")
    search_fields = ("title", "body", "dedupe_key")
    readonly_fields = ("uuid", "created_at", "updated_at")
    inlines = [DeliveryInline]


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "push_enabled", "quiet_hours_start", "quiet_hours_end")
    search_fields = ("user__username",)


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ("notification", "subscription", "state", "status_code", "created_at")
    list_filter = ("state", "backend")
