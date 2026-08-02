from rest_framework import serializers

from apps.network.models import (
    AccidentRecord,
    CameraFeed,
    Intersection,
    RoadEvent,
    RoadSegment,
    TrafficObservation,
    TrafficSignal,
)


class IntersectionSerializer(serializers.ModelSerializer):
    label = serializers.CharField(read_only=True)
    has_signal = serializers.SerializerMethodField()

    class Meta:
        model = Intersection
        fields = [
            "id", "osm_id", "name", "label", "city", "latitude", "longitude",
            "is_signalised", "base_delay_s", "has_signal",
        ]

    def get_has_signal(self, obj) -> bool:
        return hasattr(obj, "signal")


class RoadSegmentSerializer(serializers.ModelSerializer):
    from_node_name = serializers.CharField(source="from_node.label", read_only=True)
    to_node_name = serializers.CharField(source="to_node.label", read_only=True)
    travel_time_s = serializers.SerializerMethodField()

    class Meta:
        model = RoadSegment
        fields = [
            "id", "name", "from_node", "to_node", "from_node_name", "to_node_name",
            "road_class", "length_m", "lanes", "free_flow_kmh", "is_open",
            "allows_contraflow", "current_speed_kmh", "congestion_level",
            "congestion_index", "speed_updated_at", "travel_time_s",
            "latitude", "longitude",
        ]
        read_only_fields = ["congestion_level", "congestion_index", "speed_updated_at"]

    def get_travel_time_s(self, obj) -> float:
        return round(obj.travel_time_s(), 1)


class RoadSegmentGeoSerializer(serializers.ModelSerializer):
    """GeoJSON feature form, used to paint the network on the map."""

    class Meta:
        model = RoadSegment
        fields = ["id"]

    def to_representation(self, obj):
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": obj.geojson_coordinates()},
            "properties": {
                "id": obj.id,
                "name": obj.name,
                "road_class": obj.road_class,
                "congestion_level": obj.congestion_level,
                "congestion_index": round(obj.congestion_index, 3),
                "speed_kmh": obj.current_speed_kmh,
                "is_open": obj.is_open,
            },
        }


class TrafficSignalSerializer(serializers.ModelSerializer):
    intersection_name = serializers.CharField(source="intersection.label", read_only=True)
    latitude = serializers.FloatField(read_only=True)
    longitude = serializers.FloatField(read_only=True)

    class Meta:
        model = TrafficSignal
        fields = [
            "id", "uuid", "controller_id", "controller_type", "intersection",
            "intersection_name", "latitude", "longitude", "cycle_seconds",
            "current_phase", "is_preempted", "preempted_until", "supports_preemption",
            "min_recovery_s", "is_online", "last_heartbeat",
        ]
        read_only_fields = ["is_preempted", "preempted_until", "current_phase"]


class CameraFeedSerializer(serializers.ModelSerializer):
    class Meta:
        model = CameraFeed
        fields = [
            "id", "name", "latitude", "longitude", "intersection", "segment",
            "stream_url", "heading_deg", "is_active", "last_analysed_at",
        ]


class TrafficObservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrafficObservation
        fields = [
            "id", "segment", "observed_at", "speed_kmh", "vehicle_count",
            "density", "occupancy", "congestion_level", "source", "camera",
        ]


class RoadEventSerializer(serializers.ModelSerializer):
    event_type_display = serializers.CharField(source="get_event_type_display", read_only=True)
    blocks_road = serializers.BooleanField(read_only=True)

    class Meta:
        model = RoadEvent
        fields = [
            "id", "uuid", "event_type", "event_type_display", "segment", "description",
            "severity", "confidence", "source", "starts_at", "ends_at", "is_active",
            "latitude", "longitude", "radius_m", "blocks_road",
        ]


class AccidentRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccidentRecord
        fields = [
            "id", "occurred_at", "latitude", "longitude", "severity",
            "intersection", "casualties", "description",
        ]


class SpeedUpdateSerializer(serializers.Serializer):
    """Bulk live-speed ingestion from probes or an external traffic provider."""

    segment_id = serializers.IntegerField()
    speed_kmh = serializers.FloatField(min_value=0, max_value=200)


class CameraAnalysisRequestSerializer(serializers.Serializer):
    camera_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
