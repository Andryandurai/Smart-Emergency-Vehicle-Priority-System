"""Crew takeover, pairing and the start-of-shift vehicle check.

Three facts this module exists to make answerable at any moment:

* **who is on this ambulance** - which driver, which paramedic, since when;
* **that both of them agreed to it** - the driver opens the takeover and
  names the paramedic, and the shift does not become live until the paramedic
  accepts, so neither is recorded as crewing a vehicle they never boarded;
* **whether the vehicle was checked** - and if it went out without being
  checked, that this was a deliberate emergency decision with a name and a
  timestamp against it, not a form somebody forgot.

The equipment catalogue lives here rather than in the database because it is
a fleet-wide standard rather than per-vehicle configuration; a service that
needs per-vehicle lists should move ``EQUIPMENT_CATALOGUE`` into a model
before adding conditionals to it.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.enums import ShiftStatus
from apps.core.models import TimeStampedModel, UUIDModel


#: The start-of-shift check.
#:
#: ``critical`` items are the ones whose failure makes the vehicle unfit for
#: a Level 1 response. They are not merely highlighted: a failed critical item
#: blocks the vehicle from being marked ready at all (see
#: :meth:`EquipmentCheck.derived_readiness`), which is the difference between
#: a checklist that records a problem and one that prevents a dispatch.
#:
#: ``failure_reason`` maps an item to the maintenance triage category it
#: belongs to, so a driver who fails "Brakes" does not then have to
#: separately tell the workshop that the problem is the brakes.
#:
#: Codes are permanent. Existing checks store answers keyed by code, so
#: renaming one silently discards history - add and deprecate, never rename.
EQUIPMENT_CATALOGUE: list[dict] = [
    # --- Vehicle inspection ------------------------------------------------
    {"code": "fuel_level", "label": "Fuel level", "group": "Vehicle Inspection", "critical": True, "failure_reason": "other"},
    {"code": "engine_status", "label": "Engine status", "group": "Vehicle Inspection", "critical": True, "failure_reason": "engine"},
    {"code": "battery", "label": "Battery", "group": "Vehicle Inspection", "critical": True, "failure_reason": "battery"},
    {"code": "brakes", "label": "Brakes", "group": "Vehicle Inspection", "critical": True, "failure_reason": "brakes"},
    {"code": "tyres", "label": "Tyres", "group": "Vehicle Inspection", "critical": True, "failure_reason": "tyres"},
    {"code": "steering", "label": "Steering", "group": "Vehicle Inspection", "critical": True, "failure_reason": "other"},
    {"code": "lights", "label": "Lights", "group": "Vehicle Inspection", "critical": True, "failure_reason": "other"},
    {"code": "emergency_lights", "label": "Emergency lights", "group": "Vehicle Inspection", "critical": True, "failure_reason": "emergency_lights"},
    {"code": "siren_lights", "label": "Siren", "group": "Vehicle Inspection", "critical": True, "failure_reason": "siren"},
    {"code": "gps_device", "label": "GPS device", "group": "Vehicle Inspection", "critical": True, "failure_reason": "gps"},
    {"code": "radio_gps", "label": "Communication radio", "group": "Vehicle Inspection", "critical": True, "failure_reason": "gps"},

    # --- Medical equipment -------------------------------------------------
    {"code": "oxygen_cylinder", "label": "Oxygen cylinder", "group": "Medical Equipment", "critical": True, "failure_reason": "oxygen"},
    {"code": "stretcher", "label": "Stretcher", "group": "Medical Equipment", "critical": True, "failure_reason": "medical_equipment"},
    {"code": "defibrillator", "label": "AED / Defibrillator", "group": "Medical Equipment", "critical": True, "failure_reason": "medical_equipment"},
    {"code": "trauma_kit", "label": "Trauma kit", "group": "Medical Equipment", "critical": True, "failure_reason": "medical_equipment"},
    {"code": "first_aid_kit", "label": "First aid kit", "group": "Medical Equipment", "critical": True, "failure_reason": "medical_equipment"},
    {"code": "fire_extinguisher", "label": "Fire extinguisher", "group": "Medical Equipment", "critical": True, "failure_reason": "other"},

    # --- General -----------------------------------------------------------
    # Non-critical: a dirty cabin or a lapsed photocopy is a compliance
    # problem, not a reason to leave a cardiac arrest without an ambulance.
    {"code": "cleanliness", "label": "Cleanliness", "group": "General", "critical": False, "failure_reason": "other"},
    {"code": "documents", "label": "Required documents", "group": "General", "critical": False, "failure_reason": "other"},
    {"code": "insurance", "label": "Insurance", "group": "General", "critical": False, "failure_reason": "other"},
    {"code": "registration", "label": "Registration", "group": "General", "critical": False, "failure_reason": "other"},
]

EQUIPMENT_BY_CODE = {item["code"]: item for item in EQUIPMENT_CATALOGUE}
CRITICAL_CODES = {item["code"] for item in EQUIPMENT_CATALOGUE if item["critical"]}


def failure_reasons_for(codes) -> list[str]:
    """Maintenance triage categories implied by a set of failed item codes."""
    reasons = {
        EQUIPMENT_BY_CODE[code]["failure_reason"]
        for code in codes
        if code in EQUIPMENT_BY_CODE
    }
    return sorted(reasons)


class CrewShiftQuerySet(models.QuerySet):
    def live(self):
        return self.filter(status=ShiftStatus.ACTIVE)

    def open(self):
        """Anything not yet finished with: claimed, requested or running."""
        return self.filter(
            status__in=[ShiftStatus.DRAFT, ShiftStatus.PENDING, ShiftStatus.ACTIVE]
        )

    def for_user(self, user):
        return self.filter(models.Q(driver=user) | models.Q(paramedic=user))


class CrewShift(TimeStampedModel, UUIDModel):
    """A driver and a paramedic crewing one vehicle for one shift.

    Both seats are plain users from the ambulance role rather than separate
    driver/paramedic roles: the same person drives on Monday and attends on
    Tuesday, and encoding the seat in the shift rather than the account keeps
    that true without RBAC gymnastics.
    """

    vehicle = models.ForeignKey(
        "fleet.EmergencyVehicle", on_delete=models.CASCADE, related_name="shifts"
    )
    driver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="driving_shifts"
    )
    #: Null while the shift is a DRAFT - the driver has the vehicle and is
    #: inspecting it, and has not yet named who is crewing with them.
    paramedic = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="attending_shifts",
        null=True, blank=True,
    )
    status = models.CharField(
        max_length=12, choices=ShiftStatus.choices, default=ShiftStatus.DRAFT, db_index=True
    )

    requested_at = models.DateTimeField(default=timezone.now)
    accepted_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    decline_reason = models.CharField(max_length=300, blank=True)

    objects = CrewShiftQuerySet.as_manager()

    class Meta:
        ordering = ["-requested_at"]
        indexes = [
            models.Index(fields=["vehicle", "status"]),
            models.Index(fields=["status", "-requested_at"]),
        ]
        constraints = [
            # One live crew per vehicle. Without this a second driver could
            # open a takeover on an ambulance that is already out, and the
            # question "who is on AMB-101 right now" would have two answers.
            models.UniqueConstraint(
                fields=["vehicle"],
                condition=models.Q(status__in=["draft", "pending", "active"]),
                name="one_open_shift_per_vehicle",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vehicle_id}: {self.driver_id}/{self.paramedic_id} ({self.status})"

    @property
    def is_live(self) -> bool:
        return self.status == ShiftStatus.ACTIVE

    def accept(self) -> None:
        self.status = ShiftStatus.ACTIVE
        self.accepted_at = timezone.now()
        self.save(update_fields=["status", "accepted_at", "updated_at"])

    def decline(self, reason: str = "") -> None:
        self.status = ShiftStatus.DECLINED
        self.decline_reason = reason
        self.ended_at = timezone.now()
        self.save(update_fields=["status", "decline_reason", "ended_at", "updated_at"])
        self.release_vehicle()

    def end(self) -> None:
        self.status = ShiftStatus.ENDED
        self.ended_at = timezone.now()
        self.save(update_fields=["status", "ended_at", "updated_at"])
        self.release_vehicle()

    def release_vehicle(self) -> None:
        """Hand the ambulance back to the pool the moment the crew signs off.

        Ending a shift used to close the *shift* and leave the *vehicle* where
        the last job left it - AT_HOSPITAL, RETURNING, ON_SCENE. Nothing ever
        moved it back, so an ambulance that finished a run was crewless and
        yet permanently absent from the takeover picker: the next driver saw
        an empty board and the vehicle was, in practice, retired.

        Two states are deliberately left alone. OFFLINE means the onboard unit
        is not reporting, and OUT_OF_SERVICE is somebody's explicit decision -
        neither is a vehicle a crew signing off can declare fit. Readiness is
        untouched on purpose: a grounded ambulance stays grounded, and is
        filtered out of the picker by readiness rather than by status.
        """
        from apps.core.enums import VehicleStatus

        held = {
            VehicleStatus.DISPATCHED,
            VehicleStatus.ON_SCENE,
            VehicleStatus.TRANSPORTING,
            VehicleStatus.AT_HOSPITAL,
            VehicleStatus.RETURNING,
        }
        vehicle = self.vehicle
        if vehicle.status not in held:
            return
        # Still carrying a patient. The crew signing off does not make the
        # ambulance free - the response has to finish or be cancelled first.
        if vehicle.active_trip is not None:
            return
        vehicle.status = VehicleStatus.AVAILABLE
        vehicle.save(update_fields=["status", "updated_at"])


class EquipmentCheck(TimeStampedModel):
    """The start-of-shift vehicle check for one shift.

    ``skipped`` is the important field. A crew handed an emergency call while
    still walking to the vehicle cannot stop to tick twenty boxes, and a
    system that demands it will simply be lied to - so the skip is a
    first-class, attributed action with a reason, and the check stays
    outstanding until it is completed later.
    """

    shift = models.OneToOneField(CrewShift, on_delete=models.CASCADE, related_name="equipment_check")
    #: ``{code: {"present": bool, "note": str}}`` for every catalogue item the
    #: crew answered. Absent codes are unanswered, which is not the same as
    #: answered "missing".
    items = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="equipment_checks",
    )

    skipped = models.BooleanField(default=False)
    skip_reason = models.CharField(max_length=300, blank=True)
    skipped_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"check for shift {self.shift_id}"

    # -- derived state ------------------------------------------------------
    @property
    def missing_codes(self) -> list[str]:
        return sorted(
            code for code, answer in (self.items or {}).items()
            if not (answer or {}).get("present", False)
        )

    @property
    def missing_critical(self) -> list[str]:
        return sorted(set(self.missing_codes) & CRITICAL_CODES)

    @property
    def answered_count(self) -> int:
        return len(self.items or {})

    @property
    def is_complete(self) -> bool:
        """Every catalogue item answered. A skip is not a completion."""
        return self.answered_count >= len(EQUIPMENT_CATALOGUE)

    @property
    def is_outstanding(self) -> bool:
        """Still owed - either skipped, or started and not finished."""
        return not self.is_complete

    def derived_readiness(self) -> str:
        """Fitness for dispatch, derived from the answers rather than declared.

        Readiness is not a field the driver sets. It follows from what they
        recorded, so "ready" cannot be asserted over a failed brake check -
        which is the whole point of gating dispatch on the inspection rather
        than merely logging one.
        """
        from apps.core.enums import VehicleReadiness

        if self.missing_critical:
            return VehicleReadiness.NOT_READY
        if self.is_complete:
            return VehicleReadiness.READY
        if self.skipped:
            # Dispatchable now, inspection still owed. Deliberately its own
            # state so it can be counted and chased.
            return VehicleReadiness.TEMPORARILY_READY
        return VehicleReadiness.UNCHECKED

    @property
    def failed_reasons(self) -> list[str]:
        """Triage categories for whatever failed - feeds the maintenance report."""
        return failure_reasons_for(self.missing_codes)

    def as_payload(self) -> dict:
        return {
            "id": self.id,
            "shift_id": self.shift_id,
            "items": self.items or {},
            "answered": self.answered_count,
            "total": len(EQUIPMENT_CATALOGUE),
            "is_complete": self.is_complete,
            "is_outstanding": self.is_outstanding,
            "missing": self.missing_codes,
            "missing_critical": self.missing_critical,
            "readiness": self.derived_readiness(),
            "failed_reasons": self.failed_reasons,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "skipped_at": self.skipped_at,
            "completed_at": self.completed_at,
            "completed_by": self.completed_by.get_username() if self.completed_by else None,
            "notes": self.notes,
        }
