"""Dispatch state: trips, their routes, their corridors, their siren mode.

This app is the spine that ties the layers together.  A trip carries the
patient's clinical category (Layer 5), owns the active route (Layer 2), the
signal preemptions derived from it (Layer 3) and the light/siren directives
derived from severity (Layer 6).
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.core.enums import (
    EmergencyCategory,
    HospitalChoiceReason,
    LightPattern,
    PatientSymptom,
    PreemptionState,
    PriorityLevel,
    SirenMode,
    TripStage,
)
from apps.core.geo import Point
from apps.core.models import TimeStampedModel, UUIDModel

TERMINAL_STAGES = [TripStage.ARRIVED, TripStage.HANDOVER, TripStage.CANCELLED]


class EmergencyTripQuerySet(models.QuerySet):
    def active(self):
        return self.exclude(stage__in=TERMINAL_STAGES)

    def transporting(self):
        return self.filter(stage=TripStage.TO_HOSPITAL)

    def inbound_to(self, hospital):
        return self.active().filter(destination_hospital=hospital)


class EmergencyTrip(TimeStampedModel, UUIDModel):
    """One emergency response, from call-out to patient handover."""

    reference = models.CharField(max_length=24, unique=True, db_index=True)
    vehicle = models.ForeignKey(
        "fleet.EmergencyVehicle", on_delete=models.PROTECT, related_name="trips"
    )
    stage = models.CharField(
        max_length=16, choices=TripStage.choices, default=TripStage.CREATED, db_index=True
    )

    # --- scene ------------------------------------------------------------
    incident_latitude = models.FloatField(null=True, blank=True)
    incident_longitude = models.FloatField(null=True, blank=True)
    incident_address = models.CharField(max_length=300, blank=True)
    caller_number = models.CharField(max_length=32, blank=True)

    # --- clinical (Layer 5) ------------------------------------------------
    emergency_category = models.CharField(
        max_length=20, choices=EmergencyCategory.choices, default=EmergencyCategory.UNKNOWN,
        db_index=True,
    )
    patient_age = models.PositiveSmallIntegerField(null=True, blank=True)
    patient_notes = models.TextField(blank=True)
    patient_deteriorating = models.BooleanField(default=False)
    #: Observed symptoms - PatientSymptom codes. Recorded alongside the
    #: category rather than instead of it: they are what the crew can see, and
    #: they are what the receiving hospital most wants in advance. When the
    #: category is UNDETERMINED these drive hospital matching on their own -
    #: see apps/hospitals/symptoms.py.
    symptoms = models.JSONField(default=list, blank=True)
    #: Recorded whenever the crew re-triages, so Layer 6 can react.
    condition_updated_at = models.DateTimeField(null=True, blank=True)

    # --- destination -------------------------------------------------------
    destination_hospital = models.ForeignKey(
        "hospitals.Hospital", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="incoming_trips",
    )
    destination_latitude = models.FloatField(null=True, blank=True)
    destination_longitude = models.FloatField(null=True, blank=True)
    hospital_was_overridden = models.BooleanField(default=False)
    #: Why this hospital. Separates "the crew disagreed with the engine" from
    #: "the patient exercised their right to choose", which review must not
    #: conflate - see HospitalChoiceReason.
    hospital_choice_reason = models.CharField(
        max_length=20,
        choices=HospitalChoiceReason.choices,
        default=HospitalChoiceReason.RECOMMENDED,
    )
    hospital_choice_note = models.CharField(max_length=300, blank=True)

    # --- Layer 6 -----------------------------------------------------------
    priority_level = models.PositiveSmallIntegerField(
        choices=PriorityLevel.choices, default=PriorityLevel.MODERATE, db_index=True
    )
    siren_mode = models.CharField(max_length=16, choices=SirenMode.choices, default=SirenMode.OFF)
    light_pattern = models.CharField(
        max_length=16, choices=LightPattern.choices, default=LightPattern.OFF
    )
    allow_contraflow = models.BooleanField(default=False)

    # --- lifecycle timestamps (the raw material for response analytics) ----
    dispatched_at = models.DateTimeField(null=True, blank=True)
    arrived_scene_at = models.DateTimeField(null=True, blank=True)
    departed_scene_at = models.DateTimeField(null=True, blank=True)
    arrived_hospital_at = models.DateTimeField(null=True, blank=True)
    handover_at = models.DateTimeField(null=True, blank=True)
    #: When the receiving hospital took the patient in and stood its resources
    #: down. Distinct from `handover_at`: handover is the crew's job ending,
    #: admission is the ward committing a bed - and only the second one changes
    #: what the recommender sees. Also the idempotency guard, so a double-tap
    #: on "Admit patient" cannot decrement the same beds twice.
    admitted_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=200, blank=True)

    #: Latest computed ETA, denormalised so dashboards need no recomputation.
    eta = models.DateTimeField(null=True, blank=True)
    distance_remaining_m = models.FloatField(null=True, blank=True)

    objects = EmergencyTripQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["stage", "-created_at"]),
            models.Index(fields=["destination_hospital", "stage"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} [{self.get_stage_display()}]"

    #: How many times to retry a colliding reference before giving up. Each
    #: retry re-reads the current maximum, so contention resolves in one or
    #: two attempts even under heavy concurrent dispatch.
    REFERENCE_MAX_ATTEMPTS = 8

    def save(self, *args, **kwargs):
        if self.reference:
            return super().save(*args, **kwargs)
        return self._save_with_generated_reference(*args, **kwargs)

    def _save_with_generated_reference(self, *args, **kwargs):
        """Allocate a daily sequential reference, safely under concurrency.

        The obvious implementation - read the current maximum, add one, write
        - is a read-then-write race. On SQLite its write serialisation hides
        it; on PostgreSQL with several ASGI workers two simultaneous
        call-outs read the same maximum and the second violates the unique
        constraint on ``reference``.

        Rather than reach for a database sequence (which would not reset per
        day, and would leave gaps that a control room reads as lost
        incidents), the insert is retried against the unique constraint. The
        constraint is the authority; this loop just re-derives a candidate
        when it loses. Each attempt runs in its own savepoint so a failed
        insert does not poison the caller's transaction.
        """
        from django.db import IntegrityError, transaction

        last_error: Exception | None = None
        for _ in range(self.REFERENCE_MAX_ATTEMPTS):
            self.reference = self._next_reference()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError as exc:
                if "reference" not in str(exc).lower():
                    raise            # a different constraint - not ours to retry
                last_error = exc
                # Django marks the instance as saved on a failed INSERT path;
                # clear it so the retry inserts rather than updates.
                self.pk = None
                self._state.adding = True

        raise IntegrityError(
            f"Could not allocate a unique trip reference after "
            f"{self.REFERENCE_MAX_ATTEMPTS} attempts"
        ) from last_error

    @staticmethod
    def _next_reference() -> str:
        """Next candidate reference, e.g. ``SEV-20260802-0007``."""
        today = timezone.localdate()
        prefix = f"SEV-{today:%Y%m%d}-"
        last = (
            EmergencyTrip.objects.filter(reference__startswith=prefix)
            .order_by("-reference")
            .values_list("reference", flat=True)
            .first()
        )
        try:
            sequence = int(last.rsplit("-", 1)[1]) + 1 if last else 1
        except (ValueError, IndexError):     # hand-edited reference in the table
            sequence = EmergencyTrip.objects.filter(reference__startswith=prefix).count() + 1
        return f"{prefix}{sequence:04d}"

    # -- geometry helpers ---------------------------------------------------
    @property
    def incident_point(self) -> Point | None:
        if self.incident_latitude is None:
            return None
        return Point(self.incident_latitude, self.incident_longitude)

    @property
    def destination_point(self) -> Point | None:
        if self.destination_latitude is None:
            return None
        return Point(self.destination_latitude, self.destination_longitude)

    @property
    def active_route(self):
        """The route the vehicle is currently following, if any."""
        return self.routes.filter(is_active=True).order_by("-computed_at").first()

    @property
    def is_active(self) -> bool:
        return self.stage not in TERMINAL_STAGES

    @property
    def is_transporting_patient(self) -> bool:
        return self.stage == TripStage.TO_HOSPITAL

    # -- derived analytics --------------------------------------------------
    @property
    def response_time_s(self) -> float | None:
        """Dispatch -> on scene: the number the health service is judged on."""
        if self.dispatched_at and self.arrived_scene_at:
            return (self.arrived_scene_at - self.dispatched_at).total_seconds()
        return None

    @property
    def transport_time_s(self) -> float | None:
        if self.departed_scene_at and self.arrived_hospital_at:
            return (self.arrived_hospital_at - self.departed_scene_at).total_seconds()
        return None

    @property
    def total_time_s(self) -> float | None:
        end = self.handover_at or self.arrived_hospital_at
        if self.dispatched_at and end:
            return (end - self.dispatched_at).total_seconds()
        return None

    @property
    def symptom_labels(self) -> list[str]:
        """Human-readable symptom names, in catalogue order.

        Ordered by the enum rather than by however the client happened to
        send them, so the same set of observations always reads the same way
        on the hospital board.
        """
        selected = set(self.symptoms or [])
        return [label for value, label in PatientSymptom.choices if value in selected]

    def as_hospital_payload(self) -> dict:
        """Exactly the fields the Hospital Preparedness Dashboard shows."""
        vehicle = self.vehicle
        return {
            "trip_id": self.id,
            "reference": self.reference,
            "vehicle": vehicle.callsign,
            "vehicle_type": vehicle.vehicle_type,
            "latitude": vehicle.latitude,
            "longitude": vehicle.longitude,
            "speed_kmh": round(vehicle.speed_kmh, 1),
            "emergency_category": self.emergency_category,
            "emergency_category_display": self.get_emergency_category_display(),
            # The observations the crew recorded. This is the single most
            # useful thing the receiving team can have before the doors open:
            # "unconscious, bleeding" lets them call the trauma bay and cross
            # match blood while the ambulance is still moving, where a bare
            # category of "Undetermined" tells them nothing to act on.
            "symptoms": list(self.symptoms or []),
            "symptom_labels": self.symptom_labels,
            "priority_level": self.priority_level,
            "stage": self.stage,
            "stage_display": self.get_stage_display(),
            "eta": self.eta,
            "distance_remaining_m": (
                round(self.distance_remaining_m, 1) if self.distance_remaining_m else None
            ),
            "patient_age": self.patient_age,
            "patient_notes": self.patient_notes,
            "patient_deteriorating": self.patient_deteriorating,
            "route_status": "on_route" if self.active_route else "no_route",
        }


class RoutePlan(TimeStampedModel):
    """A materialised route for a trip.

    Superseded plans are kept (``is_active=False``) so the ops dashboard can
    show *why* a vehicle was rerouted and analytics can measure how much the
    replans actually saved.
    """

    trip = models.ForeignKey(EmergencyTrip, on_delete=models.CASCADE, related_name="routes")
    is_active = models.BooleanField(default=True, db_index=True)
    algorithm = models.CharField(max_length=16, default="astar")
    reason = models.CharField(max_length=300, blank=True)

    origin_latitude = models.FloatField()
    origin_longitude = models.FloatField()
    destination_latitude = models.FloatField()
    destination_longitude = models.FloatField()

    #: [[lat, lon], ...] - drawn directly by the dashboards.
    geometry = models.JSONField(default=list)
    #: Serialised RouteStep dicts, including per-step predicted speeds and the
    #: signalised-exit flags the corridor planner needs.
    steps = models.JSONField(default=list)
    node_ids = models.JSONField(default=list, blank=True)

    total_distance_m = models.FloatField(default=0.0)
    total_duration_s = models.FloatField(default=0.0)
    computed_at = models.DateTimeField(default=timezone.now, db_index=True)
    predicted_eta = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-computed_at"]
        indexes = [models.Index(fields=["trip", "is_active"])]

    def __str__(self) -> str:
        return f"Route for {self.trip_id} ({self.total_duration_s / 60:.1f} min)"

    @property
    def signalised_steps(self) -> list[dict]:
        return [s for s in (self.steps or []) if s.get("is_signalised_exit")]

    def as_geojson(self) -> dict:
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [[lon, lat] for lat, lon in self.geometry],
            },
            "properties": {
                "trip_id": self.trip_id,
                "duration_s": self.total_duration_s,
                "distance_m": self.total_distance_m,
                "algorithm": self.algorithm,
            },
        }


class SignalPreemption(TimeStampedModel, UUIDModel):
    """One forced-green request against one controller (Layer 3).

    The full lifecycle is persisted - planned, armed, active, released - so a
    traffic authority can audit every second that cross traffic was held.
    """

    trip = models.ForeignKey(EmergencyTrip, on_delete=models.CASCADE, related_name="preemptions")
    signal = models.ForeignKey(
        "network.TrafficSignal", on_delete=models.CASCADE, related_name="preemptions"
    )
    state = models.CharField(
        max_length=12, choices=PreemptionState.choices, default=PreemptionState.PLANNED,
        db_index=True,
    )
    planned_green_at = models.DateTimeField()
    planned_release_at = models.DateTimeField()
    activated_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    predicted_arrival_at = models.DateTimeField(null=True, blank=True)
    actual_arrival_at = models.DateTimeField(null=True, blank=True)

    clearance_s = models.FloatField(default=8.0)
    hold_duration_s = models.FloatField(default=20.0)
    priority_score = models.FloatField(default=0.0)
    reason = models.CharField(max_length=300, blank=True)
    #: Set when this request lost an intersection conflict to another vehicle.
    yielded_to = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="yielders"
    )
    controller_response = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["planned_green_at"]
        indexes = [
            models.Index(fields=["state", "planned_green_at"]),
            models.Index(fields=["trip", "state"]),
        ]

    def __str__(self) -> str:
        return f"Preempt {self.signal_id} for trip {self.trip_id} [{self.state}]"

    @property
    def is_open(self) -> bool:
        return self.state in {PreemptionState.PLANNED, PreemptionState.ARMED, PreemptionState.ACTIVE}

    @property
    def actual_hold_s(self) -> float | None:
        if self.activated_at and self.released_at:
            return (self.released_at - self.activated_at).total_seconds()
        return None

    @property
    def eta_error_s(self) -> float | None:
        """How wrong the arrival prediction was - the key accuracy metric."""
        if self.predicted_arrival_at and self.actual_arrival_at:
            return (self.actual_arrival_at - self.predicted_arrival_at).total_seconds()
        return None


class PriorityDirective(TimeStampedModel):
    """Layer 6 decision record: severity -> lights, siren, signal priority.

    Every upgrade and downgrade is logged with its trigger, which is what
    makes "the AI continuously monitors patient condition ... automatically
    upgrades or downgrades" auditable rather than mysterious.
    """

    trip = models.ForeignKey(EmergencyTrip, on_delete=models.CASCADE, related_name="directives")
    priority_level = models.PositiveSmallIntegerField(choices=PriorityLevel.choices)
    previous_level = models.PositiveSmallIntegerField(
        choices=PriorityLevel.choices, null=True, blank=True
    )
    siren_mode = models.CharField(max_length=16, choices=SirenMode.choices)
    light_pattern = models.CharField(max_length=16, choices=LightPattern.choices)
    grants_green_corridor = models.BooleanField(default=False)
    trigger = models.CharField(max_length=200)
    rationale = models.CharField(max_length=400, blank=True)
    issued_by = models.CharField(max_length=60, default="sevps-layer6")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Trip {self.trip_id} -> L{self.priority_level} ({self.siren_mode})"

    @property
    def is_upgrade(self) -> bool:
        return self.previous_level is not None and self.priority_level < self.previous_level

    @property
    def is_downgrade(self) -> bool:
        return self.previous_level is not None and self.priority_level > self.previous_level
