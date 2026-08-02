from django.contrib import admin

from apps.alerts.models import DisplayBoard, DriverAlert, DriverDevice


@admin.register(DisplayBoard)
class DisplayBoardAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "channel", "is_active", "current_message", "message_expires_at")
    list_filter = ("channel", "is_active")
    search_fields = ("code", "name")
    raw_id_fields = ("segment",)


@admin.register(DriverDevice)
class DriverDeviceAdmin(admin.ModelAdmin):
    list_display = ("device_id", "channel", "geohash", "speed_kmh", "last_seen_at", "is_active")
    list_filter = ("channel", "is_active")
    search_fields = ("device_id", "geohash")


@admin.register(DriverAlert)
class DriverAlertAdmin(admin.ModelAdmin):
    list_display = ("message", "trip", "channel", "eta_seconds", "expires_at", "delivered_count")
    list_filter = ("channel", "priority_level")
    raw_id_fields = ("trip", "board")
