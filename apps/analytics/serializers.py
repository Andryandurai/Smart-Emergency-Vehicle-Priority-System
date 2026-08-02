from rest_framework import serializers

from apps.analytics.models import DailyMetric, Hotspot


class DailyMetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyMetric
        fields = "__all__"


class HotspotSerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    intersection_name = serializers.CharField(
        source="intersection.name", read_only=True, default=None
    )

    class Meta:
        model = Hotspot
        fields = [
            "id", "kind", "kind_display", "label", "latitude", "longitude",
            "intersection", "intersection_name", "incident_count", "score",
            "window_days", "details", "updated_at",
        ]
