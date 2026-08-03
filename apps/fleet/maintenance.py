"""Vehicle faults, and the handover when one happens mid-transport.

Two related but distinct things live here:

:class:`MaintenanceReport`
    A fault found *before* the vehicle goes out - a failed readiness check, or
    a driver reporting a problem at the station. It grounds the vehicle and
    puts a dated, attributed record in front of fleet management.

:class:`BreakdownEvent` / :class:`TransferOffer`
    A fault found *during* a transport, which is a different emergency
    entirely: there is a patient on board. The event broadcasts to nearby
    crews, and the first to accept becomes the replacement. Offers are
    modelled explicitly rather than inferred from "whoever answered first",
    because who was asked and who declined is exactly what an incident review
    needs afterwards.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.enums import BreakdownState, MaintenanceState, TransferOfferState
from apps.core.models import TimeStampedModel, UUIDModel


class MaintenanceReport(TimeStampedModel, UUIDModel):
    """A fault that grounds a vehicle until fleet management clears it."""

    vehicle = models.ForeignKey(
        "fleet.EmergencyVehicle", on_delete=models.CASCADE, related_name="maintenance_reports"
    )
    shift = models.ForeignKey(
        "fleet.CrewShift", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="maintenance_reports",
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="maintenance_reports",
    )
    #: FailureReason codes. A list because a vehicle rarely fails one thing.
    reasons = models.JSONField(default=list, blank=True)
    #: Checklist item codes that failed, when the report came from an
    #: inspection. Kept alongside ``reasons`` because "brakes" is what the
    #: workshop triages on and "brakes, tyres" is what the driver ticked.
    failed_items = models.JSONField(default=list, blank=True)
    remarks = models.TextField(blank=True)
    state = models.CharField(
        max_length=16, choices=MaintenanceState.choices, default=MaintenanceState.OPEN,
        db_index=True,
    )
    #: Set when the report came from a failed readiness check rather than
    #: being raised by hand - the two need different follow-up.
    from_inspection = models.BooleanField(default=False)

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="acknowledged_maintenance",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["vehicle", "-created_at"]),
            models.Index(fields=["state", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.vehicle_id}: {', '.join(self.reasons) or 'fault'} ({self.state})"

    @property
    def is_open(self) -> bool:
        return self.state in {MaintenanceState.OPEN, MaintenanceState.ACKNOWLEDGED,
                              MaintenanceState.IN_PROGRESS}

    def as_payload(self) -> dict:
        return {
            "id": self.id,
            "uuid": str(self.uuid),
            "vehicle": self.vehicle.callsign,
            "registration": self.vehicle.registration,
            "reasons": list(self.reasons or []),
            "failed_items": list(self.failed_items or []),
            "remarks": self.remarks,
            "state": self.state,
            "state_display": self.get_state_display(),
            "from_inspection": self.from_inspection,
            "reported_by": self.reported_by.get_username() if self.reported_by else None,
            "reported_at": self.created_at,
            "acknowledged_at": self.acknowledged_at,
            "resolved_at": self.resolved_at,
            "is_open": self.is_open,
        }


class BreakdownEvent(TimeStampedModel, UUIDModel):
    """An ambulance failing with a patient on board.

    Carries the clinical context alongside the location, because a crew being
    asked to take over needs to know what they are taking over - a Level 1
    cardiac patient and a routine transfer are not the same offer.
    """

    trip = models.ForeignKey(
        "dispatch.EmergencyTrip", on_delete=models.CASCADE, related_name="breakdowns"
    )
    vehicle = models.ForeignKey(
        "fleet.EmergencyVehicle", on_delete=models.CASCADE, related_name="breakdowns"
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="breakdowns",
    )
    reasons = models.JSONField(default=list, blank=True)
    remarks = models.TextField(blank=True)

    latitude = models.FloatField()
    longitude = models.FloatField()

    state = models.CharField(
        max_length=20, choices=BreakdownState.choices, default=BreakdownState.OPEN,
        db_index=True,
    )
    replacement_vehicle = models.ForeignKey(
        "fleet.EmergencyVehicle", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="breakdown_rescues",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    transferred_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["state", "-created_at"]),
            models.Index(fields=["trip", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"breakdown {self.vehicle_id} on trip {self.trip_id} ({self.state})"

    @property
    def is_open(self) -> bool:
        return self.state == BreakdownState.OPEN

    def as_payload(self) -> dict:
        trip = self.trip
        return {
            "id": self.id,
            "uuid": str(self.uuid),
            "state": self.state,
            "state_display": self.get_state_display(),
            "vehicle": self.vehicle.callsign,
            "registration": self.vehicle.registration,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "reasons": list(self.reasons or []),
            "remarks": self.remarks,
            "reported_at": self.created_at,
            "trip_id": trip.id,
            "reference": trip.reference,
            # The clinical picture the receiving crew needs to decide.
            "emergency_category": trip.emergency_category,
            "emergency_category_display": trip.get_emergency_category_display(),
            "priority_level": trip.priority_level,
            "destination_hospital": (
                trip.destination_hospital.name if trip.destination_hospital else None
            ),
            "destination_latitude": trip.destination_latitude,
            "destination_longitude": trip.destination_longitude,
            "replacement": (
                self.replacement_vehicle.callsign if self.replacement_vehicle else None
            ),
            "accepted_at": self.accepted_at,
            "transferred_at": self.transferred_at,
        }


class TransferOffer(TimeStampedModel):
    """One nearby crew asked to take over a broken-down ambulance's patient.

    Recorded per-vehicle rather than broadcast-and-forget so that "nobody
    came" can be distinguished from "nobody was asked", which are very
    different findings in a review.
    """

    breakdown = models.ForeignKey(
        BreakdownEvent, on_delete=models.CASCADE, related_name="offers"
    )
    vehicle = models.ForeignKey(
        "fleet.EmergencyVehicle", on_delete=models.CASCADE, related_name="transfer_offers"
    )
    distance_m = models.FloatField(default=0.0)
    state = models.CharField(
        max_length=12, choices=TransferOfferState.choices,
        default=TransferOfferState.OFFERED, db_index=True,
    )
    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="transfer_responses",
    )
    reject_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["distance_m"]
        constraints = [
            models.UniqueConstraint(
                fields=["breakdown", "vehicle"], name="one_offer_per_vehicle_per_breakdown"
            ),
        ]

    def __str__(self) -> str:
        return f"offer {self.vehicle_id} <- breakdown {self.breakdown_id} ({self.state})"

    def respond(self, state: str, user=None, reason: str = "") -> None:
        self.state = state
        self.responded_at = timezone.now()
        self.responded_by = user
        self.reject_reason = reason
        self.save(update_fields=["state", "responded_at", "responded_by",
                                 "reject_reason", "updated_at"])

    def as_payload(self) -> dict:
        return {
            "id": self.id,
            "breakdown_id": self.breakdown_id,
            "vehicle": self.vehicle.callsign,
            "distance_m": round(self.distance_m, 1),
            "state": self.state,
            "state_display": self.get_state_display(),
            "responded_at": self.responded_at,
            "reject_reason": self.reject_reason,
        }
