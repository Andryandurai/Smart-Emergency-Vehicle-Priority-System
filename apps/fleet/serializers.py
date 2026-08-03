from rest_framework import serializers

from apps.fleet.models import EmergencyVehicle, Station, VehicleTelemetry


class StationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Station
        fields = ["id", "name", "code", "city", "address", "contact_number", "latitude", "longitude"]


class EmergencyVehicleSerializer(serializers.ModelSerializer):
    vehicle_type_display = serializers.CharField(source="get_vehicle_type_display", read_only=True)
    ownership_display = serializers.CharField(source="get_ownership_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    is_stale = serializers.BooleanField(read_only=True)
    home_station_name = serializers.CharField(source="home_station.name", read_only=True, default=None)
    active_trip_id = serializers.SerializerMethodField()

    class Meta:
        model = EmergencyVehicle
        fields = [
            "id", "uuid", "callsign", "registration", "vehicle_type", "vehicle_type_display",
            "ownership", "ownership_display",
            "operator", "home_station", "home_station_name", "status", "status_display",
            "latitude", "longitude", "heading_deg", "speed_kmh", "accuracy_m",
            "last_seen_at", "is_stale", "priority_level", "siren_mode", "light_pattern",
            "is_als", "crew_size", "equipment", "active_trip_id",
        ]
        read_only_fields = [
            "uuid", "last_seen_at", "priority_level", "siren_mode", "light_pattern",
        ]

    def get_active_trip_id(self, obj):
        trip = obj.active_trip
        return trip.id if trip else None


class VehicleTelemetrySerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleTelemetry
        fields = [
            "id", "vehicle", "trip", "latitude", "longitude",
            "speed_kmh", "heading_deg", "accuracy_m", "recorded_at",
        ]
        read_only_fields = ["vehicle", "trip"]


class TelemetryIngestSerializer(serializers.Serializer):
    """One GPS fix posted by the onboard unit / mobile app.

    ``speed_kmh`` and ``heading_deg`` are optional - SEVPS derives them from
    consecutive fixes when the device does not supply them.
    """

    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    speed_kmh = serializers.FloatField(required=False, min_value=0, max_value=250)
    heading_deg = serializers.FloatField(required=False, min_value=0, max_value=360)
    accuracy_m = serializers.FloatField(required=False, min_value=0)
    recorded_at = serializers.DateTimeField(required=False)


class VehicleStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[c[0] for c in EmergencyVehicle._meta.get_field("status").choices])
