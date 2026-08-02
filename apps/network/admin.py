from django.contrib import admin

from apps.network.models import (
    AccidentRecord,
    CameraFeed,
    Intersection,
    RoadEvent,
    RoadSegment,
    TrafficObservation,
    TrafficProfile,
    TrafficSignal,
)


@admin.register(Intersection)
class IntersectionAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "city", "is_signalised", "latitude", "longitude")
    list_filter = ("city", "is_signalised")
    search_fields = ("name", "osm_id")


@admin.register(RoadSegment)
class RoadSegmentAdmin(admin.ModelAdmin):
    list_display = (
        "id", "name", "road_class", "length_m", "lanes",
        "current_speed_kmh", "congestion_level", "is_open",
    )
    list_filter = ("road_class", "congestion_level", "is_open")
    search_fields = ("name",)
    raw_id_fields = ("from_node", "to_node")


@admin.register(TrafficSignal)
class TrafficSignalAdmin(admin.ModelAdmin):
    list_display = (
        "controller_id", "intersection", "controller_type", "current_phase",
        "is_preempted", "is_online", "last_heartbeat",
    )
    list_filter = ("controller_type", "is_preempted", "is_online", "supports_preemption")
    search_fields = ("controller_id", "intersection__name")
    raw_id_fields = ("intersection",)


@admin.register(CameraFeed)
class CameraFeedAdmin(admin.ModelAdmin):
    list_display = ("name", "segment", "is_active", "last_analysed_at")
    list_filter = ("is_active",)
    search_fields = ("name",)
    raw_id_fields = ("intersection", "segment")


@admin.register(TrafficObservation)
class TrafficObservationAdmin(admin.ModelAdmin):
    list_display = ("segment", "observed_at", "speed_kmh", "vehicle_count", "congestion_level", "source")
    list_filter = ("source", "congestion_level")
    raw_id_fields = ("segment", "camera")


@admin.register(TrafficProfile)
class TrafficProfileAdmin(admin.ModelAdmin):
    list_display = ("segment", "weekday", "hour", "speed_factor", "sample_count")
    list_filter = ("weekday", "hour")
    raw_id_fields = ("segment",)


@admin.register(RoadEvent)
class RoadEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "description", "severity", "source", "is_active", "starts_at")
    list_filter = ("event_type", "source", "is_active")
    raw_id_fields = ("segment",)


@admin.register(AccidentRecord)
class AccidentRecordAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "severity", "casualties", "intersection")
    list_filter = ("severity",)
    raw_id_fields = ("intersection",)
