"""Layer 5 - hospitals, their live capability state, and the rule base.

The recommendation is *rule-based on the clinical side and scored on the
logistics side*, which is exactly the split the problem statement asks for:
the paramedic picks a category, a hard rule says which facilities that
category requires, and only hospitals that satisfy the rule are then ranked
on travel time, capacity and workload.  A hospital can never be recommended
because it is close if it cannot treat the patient.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.core.enums import EmergencyCategory, HospitalFacility, PriorityLevel
from apps.core.models import GeoPointModel, TimeStampedModel, UUIDModel


class Hospital(TimeStampedModel, UUIDModel, GeoPointModel):
    """A receiving facility."""

    name = models.CharField(max_length=180)
    code = models.CharField(max_length=24, unique=True, db_index=True)
    city = models.CharField(max_length=80, default="Chennai", db_index=True)
    address = models.CharField(max_length=300, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    emergency_phone = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    is_trauma_designated = models.BooleanField(default=False)
    #: 0..1 quality/outcome index from the health authority's own rating.
    quality_index = models.FloatField(default=0.7)
    #: Set when the hospital declares it cannot accept new emergency arrivals.
    is_on_diversion = models.BooleanField(default=False, db_index=True)
    diversion_reason = models.CharField(max_length=200, blank=True)
    staff_group = models.ForeignKey(
        "auth.Group", on_delete=models.SET_NULL, null=True, blank=True, related_name="hospitals"
    )

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["city", "is_active"])]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

    @property
    def facility_codes(self) -> set[str]:
        return {
            c.facility
            for c in self.capabilities.all()
            if c.is_available
        }

    def has_facilities(self, required: set[str]) -> bool:
        return required.issubset(self.facility_codes)

    def missing_facilities(self, required: set[str]) -> set[str]:
        return set(required) - self.facility_codes

    @property
    def capacity(self) -> "HospitalCapacity":
        """Live capacity row, created on first access.

        Reads the ``select_related("capacity_row")`` cache when the caller
        prefetched it, so ranking a shortlist costs no extra queries.
        """
        try:
            return self.capacity_row
        except HospitalCapacity.DoesNotExist:
            return HospitalCapacity.objects.create(hospital=self)


class HospitalCapability(TimeStampedModel):
    """One facility a hospital holds, and whether it is usable right now.

    Availability is separate from existence: a cath lab under maintenance
    still exists but must not attract a cardiac case tonight.
    """

    hospital = models.ForeignKey(Hospital, on_delete=models.CASCADE, related_name="capabilities")
    facility = models.CharField(max_length=32, choices=HospitalFacility.choices, db_index=True)
    is_available = models.BooleanField(default=True)
    unavailable_reason = models.CharField(max_length=200, blank=True)
    #: How many simultaneous cases this facility can handle (e.g. 2 cath labs).
    units = models.PositiveSmallIntegerField(default=1)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["hospital", "facility"], name="uniq_hospital_facility")
        ]
        ordering = ["hospital", "facility"]
        verbose_name_plural = "hospital capabilities"

    def __str__(self) -> str:
        return f"{self.hospital.code}: {self.get_facility_display()}"


class HospitalCapacity(TimeStampedModel):
    """Live bed availability and workload - refreshed by the hospital HIS/dashboard."""

    hospital = models.OneToOneField(Hospital, on_delete=models.CASCADE, related_name="capacity_row")
    emergency_beds_total = models.PositiveIntegerField(default=20)
    emergency_beds_available = models.PositiveIntegerField(default=10)
    icu_beds_total = models.PositiveIntegerField(default=10)
    icu_beds_available = models.PositiveIntegerField(default=4)
    ventilators_available = models.PositiveIntegerField(default=2)
    operation_theatres_free = models.PositiveIntegerField(default=1)
    #: Patients currently waiting in the ED - the workload proxy.
    patients_waiting = models.PositiveIntegerField(default=0)
    doctors_on_duty = models.PositiveSmallIntegerField(default=3)
    reported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name_plural = "hospital capacity"

    def __str__(self) -> str:
        return f"{self.hospital.code} capacity"

    @property
    def is_stale(self) -> bool:
        """Capacity older than 30 minutes is treated as unreliable."""
        return (timezone.now() - self.reported_at).total_seconds() > 1800

    @property
    def emergency_occupancy(self) -> float:
        if not self.emergency_beds_total:
            return 1.0
        used = self.emergency_beds_total - self.emergency_beds_available
        return max(0.0, min(1.0, used / self.emergency_beds_total))

    @property
    def icu_occupancy(self) -> float:
        if not self.icu_beds_total:
            return 1.0
        used = self.icu_beds_total - self.icu_beds_available
        return max(0.0, min(1.0, used / self.icu_beds_total))

    @property
    def workload_index(self) -> float:
        """0 = idle, 1 = saturated.

        Combines waiting patients per doctor with bed occupancy; an ED with
        free beds but no doctors is still overloaded.
        """
        per_doctor = self.patients_waiting / max(1, self.doctors_on_duty)
        load = min(1.0, per_doctor / 6.0)
        return round(min(1.0, 0.55 * load + 0.45 * self.emergency_occupancy), 4)

    def can_accept(self, requires_icu: bool) -> tuple[bool, str]:
        if self.emergency_beds_available < 1:
            return False, "no emergency beds available"
        if requires_icu and self.icu_beds_available < 1:
            return False, "no ICU beds available"
        return True, "ok"


class EmergencyRule(TimeStampedModel):
    """One row of the Rule-Based Emergency Engine.

    Editable by clinical governance through the admin - the mapping from a
    presentation to required facilities is a medical decision, not a code
    change.  Seeded with the table from the problem statement.
    """

    category = models.CharField(
        max_length=20, choices=EmergencyCategory.choices, unique=True, db_index=True
    )
    display_name = models.CharField(max_length=120)
    #: Facilities the receiving hospital MUST have - a hard filter.
    required_facilities = models.JSONField(
        default=list, help_text="List of HospitalFacility codes - all are mandatory"
    )
    #: Facilities that improve suitability but are not mandatory.
    preferred_facilities = models.JSONField(default=list, blank=True)
    default_priority_level = models.PositiveSmallIntegerField(
        choices=PriorityLevel.choices, default=PriorityLevel.HIGH
    )
    requires_icu = models.BooleanField(default=False)
    #: Clinical time window in minutes (e.g. stroke thrombolysis ~ 60 min).
    golden_window_min = models.PositiveIntegerField(null=True, blank=True)
    #: Prefer the nearest capable hospital even at some capability cost.
    time_critical = models.BooleanField(default=False)
    guidance = models.TextField(blank=True, help_text="Pre-arrival instructions for the crew")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["display_name"]

    def __str__(self) -> str:
        return self.display_name

    @property
    def required_set(self) -> set[str]:
        return set(self.required_facilities or [])

    @property
    def preferred_set(self) -> set[str]:
        return set(self.preferred_facilities or [])


class HospitalAlert(TimeStampedModel, UUIDModel):
    """A pre-arrival notification pushed to a hospital (Layer 5 / feature 4.7).

    Persisted rather than fire-and-forget so a dashboard opened late still
    shows the inbound patient, and so handover timing can be audited.
    """

    hospital = models.ForeignKey(Hospital, on_delete=models.CASCADE, related_name="alerts")
    trip = models.ForeignKey(
        "dispatch.EmergencyTrip", on_delete=models.CASCADE, related_name="hospital_alerts"
    )
    emergency_category = models.CharField(max_length=20, choices=EmergencyCategory.choices)
    priority_level = models.PositiveSmallIntegerField(choices=PriorityLevel.choices)
    eta = models.DateTimeField(null=True, blank=True)
    distance_remaining_m = models.FloatField(null=True, blank=True)
    message = models.CharField(max_length=400, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.CharField(max_length=120, blank=True)
    #: Resources the hospital says it has readied.
    preparation_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["hospital", "-created_at"])]

    def __str__(self) -> str:
        return f"Alert {self.hospital.code} <- trip {self.trip_id}"

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None


class HospitalRecommendationLog(TimeStampedModel):
    """Audit trail: what was recommended, to whom, and why.

    Required for clinical governance - if a patient goes to the wrong place,
    the exact inputs and scores that produced that decision must be
    reconstructable.
    """

    trip = models.ForeignKey(
        "dispatch.EmergencyTrip", on_delete=models.CASCADE, related_name="recommendation_logs"
    )
    emergency_category = models.CharField(max_length=20, choices=EmergencyCategory.choices)
    recommended = models.ForeignKey(
        Hospital, on_delete=models.SET_NULL, null=True, related_name="recommendations"
    )
    chosen = models.ForeignKey(
        Hospital, on_delete=models.SET_NULL, null=True, blank=True, related_name="chosen_for"
    )
    override_reason = models.CharField(max_length=300, blank=True)
    #: Full ranked candidate list with per-factor scores.
    candidates = models.JSONField(default=list)
    rule_snapshot = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Recommendation for trip {self.trip_id}"

    @property
    def was_overridden(self) -> bool:
        return bool(self.chosen_id and self.chosen_id != self.recommended_id)
