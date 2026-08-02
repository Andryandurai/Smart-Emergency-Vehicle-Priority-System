from rest_framework import serializers

from apps.core.enums import PriorityLevel


class RouteQuerySerializer(serializers.Serializer):
    """Ad-hoc route request - used by the ops console and integration tests."""

    origin_lat = serializers.FloatField(min_value=-90, max_value=90)
    origin_lon = serializers.FloatField(min_value=-180, max_value=180)
    dest_lat = serializers.FloatField(min_value=-90, max_value=90)
    dest_lon = serializers.FloatField(min_value=-180, max_value=180)
    priority_level = serializers.ChoiceField(
        choices=PriorityLevel.choices, default=PriorityLevel.CRITICAL
    )
    algorithm = serializers.ChoiceField(
        choices=["astar", "dijkstra"], required=False, allow_null=True
    )
    departure_at = serializers.DateTimeField(required=False)
    allow_contraflow = serializers.BooleanField(default=False)
    include_geometry = serializers.BooleanField(default=True)


class CongestionForecastQuerySerializer(serializers.Serializer):
    minutes_ahead = serializers.IntegerField(default=10, min_value=0, max_value=180)
    segment_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    #: Restrict the forecast to a bounding box (the visible map viewport).
    bbox = serializers.ListField(
        child=serializers.FloatField(), min_length=4, max_length=4, required=False,
        help_text="[min_lat, min_lon, max_lat, max_lon]",
    )
    limit = serializers.IntegerField(default=500, min_value=1, max_value=5000)
