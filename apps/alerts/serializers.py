from rest_framework import serializers

from apps.alerts.models import DisplayBoard, DriverAlert, DriverDevice


class DisplayBoardSerializer(serializers.ModelSerializer):
    is_displaying_alert = serializers.BooleanField(read_only=True)

    class Meta:
        model = DisplayBoard
        fields = [
            "id", "code", "name", "channel", "latitude", "longitude", "facing_deg",
            "segment", "endpoint", "is_active", "current_message",
            "message_expires_at", "is_displaying_alert",
        ]


class DriverDeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverDevice
        fields = [
            "id", "device_id", "channel", "push_token", "latitude", "longitude",
            "geohash", "heading_deg", "speed_kmh", "last_seen_at", "is_active",
        ]
        read_only_fields = ["geohash", "last_seen_at"]


class DriverAlertSerializer(serializers.ModelSerializer):
    trip_reference = serializers.CharField(source="trip.reference", read_only=True)
    is_live = serializers.BooleanField(read_only=True)

    class Meta:
        model = DriverAlert
        fields = [
            "id", "uuid", "trip", "trip_reference", "channel", "board", "message",
            "instruction", "eta_seconds", "radius_m", "approach_bearing_deg",
            "priority_level", "latitude", "longitude", "geohash", "expires_at",
            "delivered_count", "is_live", "created_at",
        ]


class DevicePositionSerializer(serializers.Serializer):
    device_id = serializers.CharField(max_length=128)
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    heading_deg = serializers.FloatField(required=False, min_value=0, max_value=360)
    speed_kmh = serializers.FloatField(required=False, min_value=0)
    push_token = serializers.CharField(required=False, allow_blank=True, max_length=255)
