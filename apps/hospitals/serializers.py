from rest_framework import serializers

from apps.core.enums import EmergencyCategory
from apps.hospitals.models import (
    EmergencyRule,
    Hospital,
    HospitalAlert,
    HospitalCapability,
    HospitalCapacity,
    HospitalRecommendationLog,
)


class HospitalCapabilitySerializer(serializers.ModelSerializer):
    facility_display = serializers.CharField(source="get_facility_display", read_only=True)

    class Meta:
        model = HospitalCapability
        fields = [
            "id", "hospital", "facility", "facility_display",
            "is_available", "unavailable_reason", "units", "notes",
        ]


class HospitalCapacitySerializer(serializers.ModelSerializer):
    workload_index = serializers.FloatField(read_only=True)
    emergency_occupancy = serializers.FloatField(read_only=True)
    icu_occupancy = serializers.FloatField(read_only=True)
    is_stale = serializers.BooleanField(read_only=True)

    class Meta:
        model = HospitalCapacity
        fields = [
            "id", "hospital", "emergency_beds_total", "emergency_beds_available",
            "icu_beds_total", "icu_beds_available", "ventilators_available",
            "operation_theatres_free", "patients_waiting", "doctors_on_duty",
            "reported_at", "workload_index", "emergency_occupancy", "icu_occupancy",
            "is_stale",
        ]
        read_only_fields = ["hospital"]


class HospitalSerializer(serializers.ModelSerializer):
    capabilities = HospitalCapabilitySerializer(many=True, read_only=True)
    capacity = HospitalCapacitySerializer(source="capacity_row", read_only=True)
    facility_codes = serializers.SerializerMethodField()

    class Meta:
        model = Hospital
        fields = [
            "id", "uuid", "name", "code", "city", "address", "phone", "emergency_phone",
            "latitude", "longitude", "is_active", "is_trauma_designated", "quality_index",
            "is_on_diversion", "diversion_reason", "capabilities", "capacity",
            "facility_codes",
        ]

    def get_facility_codes(self, obj) -> list:
        return sorted(obj.facility_codes)


class HospitalSummarySerializer(serializers.ModelSerializer):
    """Light form for map markers and pick lists."""

    class Meta:
        model = Hospital
        fields = ["id", "code", "name", "latitude", "longitude", "is_on_diversion"]


class EmergencyRuleSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = EmergencyRule
        fields = [
            "id", "category", "category_display", "display_name", "required_facilities",
            "preferred_facilities", "default_priority_level", "requires_icu",
            "golden_window_min", "time_critical", "guidance", "is_active",
        ]


class RecommendationRequestSerializer(serializers.Serializer):
    """What the paramedic app sends after the preliminary assessment."""

    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)
    emergency_category = serializers.ChoiceField(choices=EmergencyCategory.choices)
    radius_km = serializers.FloatField(required=False, min_value=1, max_value=100)
    max_candidates = serializers.IntegerField(required=False, min_value=1, max_value=25)
    exclude_hospital_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False
    )


class HospitalAlertSerializer(serializers.ModelSerializer):
    hospital_name = serializers.CharField(source="hospital.name", read_only=True)
    category_display = serializers.CharField(
        source="get_emergency_category_display", read_only=True
    )
    is_acknowledged = serializers.BooleanField(read_only=True)
    vehicle_callsign = serializers.CharField(source="trip.vehicle.callsign", read_only=True)

    class Meta:
        model = HospitalAlert
        fields = [
            "id", "uuid", "hospital", "hospital_name", "trip", "vehicle_callsign",
            "emergency_category", "category_display", "priority_level", "eta",
            "distance_remaining_m", "message", "acknowledged_at", "acknowledged_by",
            "is_acknowledged", "preparation_notes", "created_at",
        ]
        read_only_fields = ["acknowledged_at", "created_at"]


class HospitalRecommendationLogSerializer(serializers.ModelSerializer):
    was_overridden = serializers.BooleanField(read_only=True)

    class Meta:
        model = HospitalRecommendationLog
        fields = [
            "id", "trip", "emergency_category", "recommended", "chosen",
            "override_reason", "candidates", "rule_snapshot", "was_overridden",
            "created_at",
        ]
