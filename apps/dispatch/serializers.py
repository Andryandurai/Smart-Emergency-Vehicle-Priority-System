from rest_framework import serializers

from apps.core.enums import (
    EmergencyCategory,
    HospitalChoiceReason,
    PatientSymptom,
    PriorityLevel,
    TripStage,
)
from apps.dispatch.models import EmergencyTrip, PriorityDirective, RoutePlan, SignalPreemption


class RoutePlanSerializer(serializers.ModelSerializer):
    signal_count = serializers.SerializerMethodField()

    class Meta:
        model = RoutePlan
        fields = [
            "id", "trip", "is_active", "algorithm", "reason",
            "origin_latitude", "origin_longitude",
            "destination_latitude", "destination_longitude",
            "geometry", "steps", "node_ids", "total_distance_m", "total_duration_s",
            "computed_at", "predicted_eta", "signal_count",
        ]

    def get_signal_count(self, obj) -> int:
        return len(obj.signalised_steps)


class RoutePlanSummarySerializer(serializers.ModelSerializer):
    """Without the heavy `steps` payload - for list views."""

    class Meta:
        model = RoutePlan
        fields = [
            "id", "is_active", "algorithm", "reason", "geometry",
            "total_distance_m", "total_duration_s", "computed_at", "predicted_eta",
        ]


class SignalPreemptionSerializer(serializers.ModelSerializer):
    controller_id = serializers.CharField(source="signal.controller_id", read_only=True)
    intersection = serializers.CharField(source="signal.intersection.label", read_only=True)
    latitude = serializers.FloatField(source="signal.intersection.latitude", read_only=True)
    longitude = serializers.FloatField(source="signal.intersection.longitude", read_only=True)
    actual_hold_s = serializers.FloatField(read_only=True)
    eta_error_s = serializers.FloatField(read_only=True)

    class Meta:
        model = SignalPreemption
        fields = [
            "id", "uuid", "trip", "signal", "controller_id", "intersection",
            "latitude", "longitude", "state", "planned_green_at", "planned_release_at",
            "activated_at", "released_at", "predicted_arrival_at", "actual_arrival_at",
            "clearance_s", "hold_duration_s", "priority_score", "reason",
            "actual_hold_s", "eta_error_s", "controller_response",
        ]


class PriorityDirectiveSerializer(serializers.ModelSerializer):
    is_upgrade = serializers.BooleanField(read_only=True)
    is_downgrade = serializers.BooleanField(read_only=True)
    level_display = serializers.CharField(source="get_priority_level_display", read_only=True)

    class Meta:
        model = PriorityDirective
        fields = [
            "id", "trip", "priority_level", "level_display", "previous_level",
            "siren_mode", "light_pattern", "grants_green_corridor", "trigger",
            "rationale", "issued_by", "is_upgrade", "is_downgrade", "created_at",
        ]


#: Fields that identify or describe a patient, or identify the caller.
#: Redacted for any role without clinical clearance - notably traffic police,
#: who need a vehicle's priority and position to run a corridor but have no
#: business seeing a diagnosis. See apps/core/roles.py.
PHI_FIELDS = (
    "patient_age",
    "patient_notes",
    "patient_deteriorating",
    # Symptoms describe the patient's condition as directly as a diagnosis
    # does - "unconscious, bleeding" is clinical information, and the corridor
    # operator running the signals has no business seeing it.
    "symptoms",
    "symptom_labels",
    "caller_number",
    "incident_address",
)


class ClinicalRedactionMixin:
    """Blanks patient-identifying fields for callers without clearance.

    Redaction happens at serialisation rather than by swapping serializer
    classes, so there is exactly one place to audit and no risk of a new view
    picking the unredacted variant by accident.
    """

    def to_representation(self, instance):
        data = super().to_representation(instance)

        from apps.core.roles import may_view_clinical_data

        # REST passes a request; WebSocket consumers have no request and pass
        # the authenticated scope user directly. With neither, redact - an
        # unattributed serialisation must fail closed.
        request = self.context.get("request")
        user = self.context.get("user") or (request.user if request else None)

        if user is not None and may_view_clinical_data(user):
            return data

        for field in PHI_FIELDS:
            if field in data:
                data[field] = None
        data["clinical_data_redacted"] = True
        return data


class EmergencyTripSerializer(ClinicalRedactionMixin, serializers.ModelSerializer):
    vehicle_callsign = serializers.CharField(source="vehicle.callsign", read_only=True)
    vehicle_type = serializers.CharField(source="vehicle.vehicle_type", read_only=True)
    vehicle_latitude = serializers.FloatField(source="vehicle.latitude", read_only=True)
    vehicle_longitude = serializers.FloatField(source="vehicle.longitude", read_only=True)
    vehicle_speed_kmh = serializers.FloatField(source="vehicle.speed_kmh", read_only=True)
    hospital_code = serializers.CharField(
        source="destination_hospital.code", read_only=True, default=None
    )
    hospital_name = serializers.CharField(
        source="destination_hospital.name", read_only=True, default=None
    )
    stage_display = serializers.CharField(source="get_stage_display", read_only=True)
    category_display = serializers.CharField(
        source="get_emergency_category_display", read_only=True
    )
    active_route = RoutePlanSummarySerializer(read_only=True)
    response_time_s = serializers.FloatField(read_only=True)
    transport_time_s = serializers.FloatField(read_only=True)
    symptom_labels = serializers.ListField(read_only=True)
    choice_reason_display = serializers.CharField(
        source="get_hospital_choice_reason_display", read_only=True
    )
    # --- Driver module: what the receiving hospital wants to know ----------
    # Not PHI - who is driving and how the corridor is running are logistics,
    # and a charge nurse expecting an ambulance needs both.
    driver_name = serializers.SerializerMethodField()
    vehicle_registration = serializers.CharField(
        source="vehicle.registration", read_only=True
    )
    vehicle_readiness = serializers.CharField(source="vehicle.readiness", read_only=True)
    corridor_progress = serializers.SerializerMethodField()

    def get_driver_name(self, obj) -> str | None:
        shift = next(
            (s for s in obj.vehicle.shifts.all() if s.status == "active"), None
        ) if hasattr(obj.vehicle, "_prefetched_objects_cache") else (
            obj.vehicle.shifts.filter(status="active").first()
        )
        if shift is None:
            return None
        return shift.driver.get_full_name() or shift.driver.get_username()

    def get_corridor_progress(self, obj) -> dict:
        """How much of the green corridor has actually run.

        Counted rather than described: "3 of 7 junctions held" tells a
        hospital whether the ETA is being achieved, where "corridor active"
        does not.
        """
        rows = list(obj.preemptions.all()) if hasattr(
            obj, "_prefetched_objects_cache"
        ) else list(obj.preemptions.all())
        if not rows:
            return {"total": 0, "held": 0, "failed": 0, "pending": 0}
        return {
            "total": len(rows),
            "held": sum(1 for r in rows if r.state in {"active", "released"}),
            "failed": sum(1 for r in rows if r.state == "failed"),
            "pending": sum(1 for r in rows if r.state in {"planned", "armed"}),
        }

    class Meta:
        model = EmergencyTrip
        fields = [
            "id", "uuid", "reference", "vehicle", "vehicle_callsign", "vehicle_type",
            "vehicle_latitude", "vehicle_longitude", "vehicle_speed_kmh",
            "stage", "stage_display", "emergency_category", "category_display",
            "incident_latitude", "incident_longitude", "incident_address", "caller_number",
            "patient_age", "patient_notes", "patient_deteriorating",
            "symptoms", "symptom_labels",
            "destination_hospital", "hospital_code", "hospital_name",
            "destination_latitude", "destination_longitude", "hospital_was_overridden",
            "hospital_choice_reason", "choice_reason_display", "hospital_choice_note",
            "priority_level", "siren_mode", "light_pattern", "allow_contraflow",
            "eta", "distance_remaining_m", "active_route",
            "driver_name", "vehicle_registration", "vehicle_readiness",
            "corridor_progress",
            "dispatched_at", "arrived_scene_at", "departed_scene_at",
            "arrived_hospital_at", "handover_at", "cancelled_at", "cancellation_reason",
            "response_time_s", "transport_time_s", "created_at",
        ]
        read_only_fields = [
            "reference", "uuid", "priority_level", "siren_mode", "light_pattern",
            "eta", "distance_remaining_m",
        ]


class CreateTripSerializer(serializers.Serializer):
    """Control-room call-out: assign a vehicle to an incident."""

    vehicle_id = serializers.IntegerField(required=False)
    vehicle_callsign = serializers.CharField(required=False)
    incident_latitude = serializers.FloatField(min_value=-90, max_value=90)
    incident_longitude = serializers.FloatField(min_value=-180, max_value=180)
    incident_address = serializers.CharField(required=False, allow_blank=True, max_length=300)
    caller_number = serializers.CharField(required=False, allow_blank=True, max_length=32)
    emergency_category = serializers.ChoiceField(
        choices=EmergencyCategory.choices, default=EmergencyCategory.UNKNOWN
    )
    patient_age = serializers.IntegerField(required=False, min_value=0, max_value=130)
    patient_notes = serializers.CharField(required=False, allow_blank=True)
    #: Let SEVPS pick the nearest suitable vehicle when none is named.
    auto_assign = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if not (attrs.get("vehicle_id") or attrs.get("vehicle_callsign") or attrs["auto_assign"]):
            raise serializers.ValidationError(
                "Provide vehicle_id or vehicle_callsign, or set auto_assign=true."
            )
        return attrs


class AssessPatientSerializer(serializers.Serializer):
    """Paramedic's on-scene assessment - the Layer 5 / Layer 6 trigger."""

    emergency_category = serializers.ChoiceField(choices=EmergencyCategory.choices)
    #: What the crew can actually see. Always accepted, and the only clinical
    #: input when the category is UNDETERMINED.
    symptoms = serializers.ListField(
        child=serializers.ChoiceField(choices=PatientSymptom.choices),
        required=False,
        allow_empty=True,
    )
    patient_age = serializers.IntegerField(required=False, min_value=0, max_value=130)
    patient_notes = serializers.CharField(required=False, allow_blank=True)
    patient_deteriorating = serializers.BooleanField(default=False)
    #: Crew may override the recommendation; the reason is mandatory if so.
    hospital_id = serializers.IntegerField(required=False)
    override_reason = serializers.CharField(required=False, allow_blank=True, max_length=300)
    choice_reason = serializers.ChoiceField(
        choices=HospitalChoiceReason.choices, required=False,
    )
    allow_contraflow = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if attrs.get("hospital_id") and not attrs.get("override_reason"):
            raise serializers.ValidationError(
                {"override_reason": "A reason is required when overriding the recommendation."}
            )
        # An undetermined category with nothing observed gives the recommender
        # no clinical input at all - it would return the nearest emergency
        # department and call it a decision. Refuse rather than pretend.
        if (
            attrs.get("emergency_category") == EmergencyCategory.UNKNOWN
            and not attrs.get("symptoms")
        ):
            raise serializers.ValidationError(
                {
                    "symptoms": (
                        "Select at least one symptom when the emergency category is "
                        "undetermined - otherwise there is nothing to match a hospital on."
                    )
                }
            )
        return attrs


class StageChangeSerializer(serializers.Serializer):
    stage = serializers.ChoiceField(choices=TripStage.choices)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)


class ConditionUpdateSerializer(serializers.Serializer):
    """Mid-transport re-triage - drives Layer 6 upgrade/downgrade."""

    patient_deteriorating = serializers.BooleanField(required=False)
    emergency_category = serializers.ChoiceField(
        choices=EmergencyCategory.choices, required=False
    )
    priority_level = serializers.ChoiceField(choices=PriorityLevel.choices, required=False)
    notes = serializers.CharField(required=False, allow_blank=True)
