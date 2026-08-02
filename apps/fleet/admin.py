from django.contrib import admin

from apps.fleet.models import EmergencyVehicle, Station, VehicleTelemetry


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "city", "latitude", "longitude")
    search_fields = ("name", "code")


@admin.register(EmergencyVehicle)
class EmergencyVehicleAdmin(admin.ModelAdmin):
    list_display = (
        "callsign", "vehicle_type", "status", "priority_level",
        "siren_mode", "speed_kmh", "last_seen_at",
    )
    list_filter = ("vehicle_type", "status", "priority_level", "is_als")
    search_fields = ("callsign", "registration", "operator")
    readonly_fields = ("uuid", "last_seen_at")


@admin.register(VehicleTelemetry)
class VehicleTelemetryAdmin(admin.ModelAdmin):
    list_display = ("vehicle", "recorded_at", "latitude", "longitude", "speed_kmh")
    list_filter = ("vehicle",)
    raw_id_fields = ("vehicle", "trip")
    date_hierarchy = "recorded_at"
