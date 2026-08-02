from django.contrib import admin

from apps.analytics.models import DailyMetric, Hotspot


@admin.register(DailyMetric)
class DailyMetricAdmin(admin.ModelAdmin):
    list_display = (
        "date", "city", "trips_total", "trips_completed",
        "avg_response_time_s", "signals_preempted", "driver_alerts_issued",
    )
    list_filter = ("city",)
    date_hierarchy = "date"


@admin.register(Hotspot)
class HotspotAdmin(admin.ModelAdmin):
    list_display = ("kind", "label", "incident_count", "score", "window_days", "updated_at")
    list_filter = ("kind",)
    raw_id_fields = ("intersection",)
