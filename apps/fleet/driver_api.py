"""Driver module REST surface: fleet board, maintenance, breakdown transfer.

Separated from ``crew_api`` because these are three different audiences. The
fleet board is read by operations managers, maintenance by fleet management,
and the breakdown flow by whichever crew happens to be nearest - and only the
last of those is a driver action.

RBAC note: SEVPS has one ambulance role, not separate driver and paramedic
roles, because the same person drives on Monday and attends on Tuesday. The
seat is a property of the *shift*, so "only drivers may do this" is enforced
by checking the driver seat of the caller's open shift rather than by adding
a role that would be wrong half the week. See :func:`require_driver_seat`.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core import notifications
from apps.core.enums import (
    BreakdownState,
    FailureReason,
    MaintenanceState,
    TransferOfferState,
    VehicleReadiness,
    VehicleStatus,
)
from apps.core.geo import haversine_m
from apps.core.permissions import (
    IsAdministrator,
    IsAmbulanceCrew,
    IsAuthenticatedRole,
)
from apps.core.realtime import broadcast, broadcast_ops, hospital_group, vehicle_group
from apps.fleet.crew import CrewShift
from apps.fleet.maintenance import BreakdownEvent, MaintenanceReport, TransferOffer
from apps.fleet.models import EmergencyVehicle
from apps.fleet.readiness import set_readiness

log = logging.getLogger("sevps.fleet.driver")

#: How far SEVPS will look for a crew willing to take over a patient.
TRANSFER_SEARCH_RADIUS_M = 12_000
#: Offers made per breakdown. Enough to find somebody, few enough that a
#: single failure does not page the whole city.
TRANSFER_MAX_OFFERS = 6


class DriverSeatDenied(Exception):
    """Raised when the caller is not the driver of the vehicle in question."""


def require_driver_seat(user, vehicle) -> CrewShift:
    """The caller's live shift on ``vehicle``, as its driver.

    Administrators pass regardless: they can already override vehicle status
    through the fleet board, and refusing them here would only mean they had
    to do the same thing somewhere less visible.
    """
    from apps.core.roles import Role, has_role

    shift = CrewShift.objects.live().filter(vehicle=vehicle).first()
    if shift is not None and shift.driver_id == user.id:
        return shift
    if has_role(user, Role.ADMIN) or user.is_superuser:
        return shift
    raise DriverSeatDenied(
        f"Only the driver signed on to {vehicle.callsign} can do this."
    )


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
class FaultReportSerializer(serializers.Serializer):
    reasons = serializers.ListField(
        child=serializers.ChoiceField(choices=FailureReason.choices), allow_empty=False
    )
    remarks = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class BreakdownSerializer(serializers.Serializer):
    reasons = serializers.ListField(
        child=serializers.ChoiceField(choices=FailureReason.choices), allow_empty=False
    )
    remarks = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    latitude = serializers.FloatField(required=False, min_value=-90, max_value=90)
    longitude = serializers.FloatField(required=False, min_value=-180, max_value=180)


class RejectSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)


class OverrideReadinessSerializer(serializers.Serializer):
    readiness = serializers.ChoiceField(choices=VehicleReadiness.choices)
    note = serializers.CharField(required=False, allow_blank=True, max_length=300)


# ---------------------------------------------------------------------------
# Fleet board (admin)
# ---------------------------------------------------------------------------
class FleetBoardView(APIView):
    """Every ambulance, with crew, readiness, inspection and current job.

    One query for the whole board. The joins are prefetched because this is
    polled by every open admin dashboard and a lazy load per row would make
    the busiest screen in the system the slowest.
    """

    permission_classes = [IsAuthenticatedRole]

    def get(self, request):
        vehicles = (
            EmergencyVehicle.objects.select_related("home_station")
            .prefetch_related(
                "shifts__driver", "shifts__paramedic", "shifts__equipment_check",
                "trips__destination_hospital",
            )
            .order_by("callsign")
        )
        rows = [vehicle.as_fleet_row() for vehicle in vehicles]
        return Response(
            {
                "generated_at": timezone.now(),
                "count": len(rows),
                "vehicles": rows,
                "summary": _fleet_summary(rows),
            }
        )


def _fleet_summary(rows) -> dict:
    """Counts an operations manager reads before reading any row."""
    return {
        "total": len(rows),
        "ready": sum(1 for r in rows if r["readiness"] == VehicleReadiness.READY),
        "temporarily_ready": sum(
            1 for r in rows if r["readiness"] == VehicleReadiness.TEMPORARILY_READY
        ),
        "not_ready": sum(1 for r in rows if r["readiness"] == VehicleReadiness.NOT_READY),
        "maintenance": sum(
            1 for r in rows if r["readiness"] == VehicleReadiness.MAINTENANCE
        ),
        "on_duty": sum(1 for r in rows if r["shift_status"] == "active"),
        "on_call": sum(1 for r in rows if r["current_trip_reference"]),
        "inspection_pending": sum(
            1 for r in rows if r["inspection_status"] in {"skipped - pending", "not started"}
        ),
    }


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
class MaintenanceViewSet(viewsets.ReadOnlyModelViewSet):
    """Fault reports. Drivers raise them; fleet management works them."""

    queryset = MaintenanceReport.objects.select_related("vehicle", "reported_by", "shift")
    permission_classes = [IsAuthenticatedRole]

    action_permissions = {
        "report_fault": [IsAmbulanceCrew],
        "acknowledge": [IsAdministrator],
        "resolve": [IsAdministrator],
    }

    def get_permissions(self):
        classes = self.action_permissions.get(self.action, self.permission_classes)
        return [cls() for cls in classes]

    def get_serializer_class(self):  # pragma: no cover - payloads are hand-built
        return serializers.Serializer

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        if request.query_params.get("open") == "1":
            qs = qs.filter(
                state__in=[MaintenanceState.OPEN, MaintenanceState.ACKNOWLEDGED,
                           MaintenanceState.IN_PROGRESS]
            )
        if request.query_params.get("vehicle"):
            qs = qs.filter(vehicle__callsign=request.query_params["vehicle"])
        return Response({"reports": [r.as_payload() for r in qs[:200]]})

    def retrieve(self, request, *args, **kwargs):
        return Response(self.get_object().as_payload())

    @action(detail=False, methods=["post"], url_path="report")
    def report_fault(self, request):
        """A driver raising a fault outside the inspection flow."""
        callsign = request.data.get("vehicle_callsign", "")
        vehicle = EmergencyVehicle.objects.filter(callsign=callsign).first()
        if vehicle is None:
            return Response({"detail": "Unknown vehicle."}, status=status.HTTP_404_NOT_FOUND)

        try:
            shift = require_driver_seat(request.user, vehicle)
        except DriverSeatDenied as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        serializer = FaultReportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        report = MaintenanceReport.objects.create(
            vehicle=vehicle,
            shift=shift,
            reported_by=request.user,
            reasons=data["reasons"],
            remarks=data.get("remarks", ""),
            from_inspection=False,
        )
        set_readiness(vehicle, VehicleReadiness.NOT_READY)
        notifications.vehicle_not_ready(vehicle, report)
        broadcast_ops("maintenance_report", report.as_payload())
        return Response(report.as_payload(), status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        report = self.get_object()
        report.state = MaintenanceState.ACKNOWLEDGED
        report.acknowledged_at = timezone.now()
        report.acknowledged_by = request.user
        report.save(update_fields=["state", "acknowledged_at", "acknowledged_by", "updated_at"])
        broadcast_ops("maintenance_report", report.as_payload())
        return Response(report.as_payload())

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Close a fault and return the vehicle to service."""
        report = self.get_object()
        report.state = MaintenanceState.RESOLVED
        report.resolved_at = timezone.now()
        report.resolution_notes = request.data.get("notes", "")
        report.save(
            update_fields=["state", "resolved_at", "resolution_notes", "updated_at"]
        )

        # Only un-ground the vehicle if nothing else is still open against it.
        still_open = MaintenanceReport.objects.filter(
            vehicle=report.vehicle,
            state__in=[MaintenanceState.OPEN, MaintenanceState.ACKNOWLEDGED,
                       MaintenanceState.IN_PROGRESS],
        ).exists()
        if not still_open:
            set_readiness(report.vehicle, VehicleReadiness.UNCHECKED)

        broadcast_ops("maintenance_report", report.as_payload())
        return Response({**report.as_payload(), "vehicle_returned_to_service": not still_open})


class OverrideReadinessView(APIView):
    """Administrator override of a vehicle's readiness.

    Exists because the checklist cannot know everything - a fault cleared at
    the roadside, a vehicle wrongly grounded by a mis-tap. The override is
    attributed and broadcast rather than silent, so the board shows that a
    human made the call.
    """

    permission_classes = [IsAdministrator]

    def post(self, request, callsign: str):
        vehicle = EmergencyVehicle.objects.filter(callsign=callsign).first()
        if vehicle is None:
            return Response({"detail": "Unknown vehicle."}, status=status.HTTP_404_NOT_FOUND)

        serializer = OverrideReadinessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        set_readiness(vehicle, data["readiness"])
        log.info(
            "%s readiness overridden to %s by %s: %s",
            vehicle.callsign, data["readiness"], request.user.get_username(),
            data.get("note", ""),
        )
        return Response(vehicle.as_fleet_row())


# ---------------------------------------------------------------------------
# Breakdown and patient transfer
# ---------------------------------------------------------------------------
class BreakdownViewSet(viewsets.ReadOnlyModelViewSet):
    """An ambulance failing mid-transport, and the handover that follows."""

    queryset = BreakdownEvent.objects.select_related(
        "trip", "trip__destination_hospital", "vehicle", "replacement_vehicle"
    ).prefetch_related("offers__vehicle")
    permission_classes = [IsAuthenticatedRole]

    action_permissions = {
        "declare": [IsAmbulanceCrew],
        "accept": [IsAmbulanceCrew],
        "reject": [IsAmbulanceCrew],
    }

    def get_permissions(self):
        classes = self.action_permissions.get(self.action, self.permission_classes)
        return [cls() for cls in classes]

    def get_serializer_class(self):  # pragma: no cover
        return serializers.Serializer

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        if request.query_params.get("open") == "1":
            qs = qs.filter(state=BreakdownState.OPEN)
        return Response(
            {
                "breakdowns": [
                    {**b.as_payload(), "offers": [o.as_payload() for o in b.offers.all()]}
                    for b in qs[:50]
                ]
            }
        )

    def retrieve(self, request, *args, **kwargs):
        breakdown = self.get_object()
        return Response(
            {
                **breakdown.as_payload(),
                "offers": [o.as_payload() for o in breakdown.offers.all()],
            }
        )

    @action(detail=False, methods=["get"], url_path="offers")
    def my_offers(self, request):
        """Transfer requests waiting on the vehicles this user crews.

        Polled as well as pushed: a crew that had the app backgrounded when
        the socket event fired must still see the request when they open it.
        """
        vehicle_ids = list(
            CrewShift.objects.live().for_user(request.user).values_list("vehicle_id", flat=True)
        )
        offers = (
            TransferOffer.objects.filter(
                vehicle_id__in=vehicle_ids, state=TransferOfferState.OFFERED,
                breakdown__state=BreakdownState.OPEN,
            )
            .select_related("breakdown", "breakdown__trip", "vehicle")
            .order_by("distance_m")
        )
        return Response(
            {
                "offers": [
                    {**offer.as_payload(), "breakdown": offer.breakdown.as_payload()}
                    for offer in offers
                ]
            }
        )

    @action(detail=False, methods=["post"])
    def declare(self, request):
        """The driver's Emergency Breakdown button.

        Everything downstream happens from this one press, because a crew
        with a deteriorating patient and a dead engine cannot be asked to
        also notify four parties.
        """
        callsign = request.data.get("vehicle_callsign", "")
        vehicle = EmergencyVehicle.objects.filter(callsign=callsign).first()
        if vehicle is None:
            return Response({"detail": "Unknown vehicle."}, status=status.HTTP_404_NOT_FOUND)

        try:
            require_driver_seat(request.user, vehicle)
        except DriverSeatDenied as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        trip = vehicle.active_trip
        if trip is None:
            return Response(
                {
                    "detail": (
                        "This ambulance has no active response. Report a fault instead - "
                        "a breakdown is specifically an ambulance failing with a patient "
                        "on board."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = BreakdownSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            breakdown = BreakdownEvent.objects.create(
                trip=trip,
                vehicle=vehicle,
                reported_by=request.user,
                reasons=data["reasons"],
                remarks=data.get("remarks", ""),
                latitude=data.get("latitude", vehicle.latitude),
                longitude=data.get("longitude", vehicle.longitude),
            )
            MaintenanceReport.objects.create(
                vehicle=vehicle,
                reported_by=request.user,
                reasons=data["reasons"],
                remarks=f"In-transport breakdown on {trip.reference}. "
                        f"{data.get('remarks', '')}".strip(),
                from_inspection=False,
            )
            vehicle.status = VehicleStatus.OUT_OF_SERVICE
            vehicle.save(update_fields=["status", "updated_at"])
            set_readiness(vehicle, VehicleReadiness.MAINTENANCE, broadcast=False)
            offers = _offer_transfer(breakdown)

        payload = {**breakdown.as_payload(), "offers": [o.as_payload() for o in offers]}
        notifications.ambulance_breakdown(breakdown)
        broadcast_ops("ambulance_breakdown", payload)
        _push_offers(breakdown, offers)

        hospital = trip.destination_hospital
        if hospital:
            broadcast(hospital_group(hospital.code), "ambulance_breakdown", payload)

        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        """First crew to accept becomes the replacement.

        The race is resolved in the database, not in the handler: the update
        is conditional on the breakdown still being OPEN, so two crews
        pressing Accept in the same second cannot both win.
        """
        breakdown = self.get_object()
        callsign = request.data.get("vehicle_callsign", "")
        vehicle = EmergencyVehicle.objects.filter(callsign=callsign).first()
        if vehicle is None:
            return Response({"detail": "Unknown vehicle."}, status=status.HTTP_404_NOT_FOUND)

        offer = TransferOffer.objects.filter(breakdown=breakdown, vehicle=vehicle).first()
        if offer is None:
            return Response(
                {"detail": "This ambulance was not offered that transfer."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            require_driver_seat(request.user, vehicle)
        except DriverSeatDenied as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)

        with transaction.atomic():
            claimed = BreakdownEvent.objects.filter(
                pk=breakdown.pk, state=BreakdownState.OPEN
            ).update(
                state=BreakdownState.TRANSFER_ACCEPTED,
                replacement_vehicle=vehicle,
                accepted_at=timezone.now(),
            )
            if not claimed:
                breakdown.refresh_from_db()
                return Response(
                    {
                        "detail": (
                            f"Another crew has already taken this transfer"
                            + (f" ({breakdown.replacement_vehicle.callsign})"
                               if breakdown.replacement_vehicle else "")
                            + "."
                        ),
                        "breakdown": breakdown.as_payload(),
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            offer.respond(TransferOfferState.ACCEPTED, request.user)
            TransferOffer.objects.filter(
                breakdown=breakdown, state=TransferOfferState.OFFERED
            ).exclude(pk=offer.pk).update(
                state=TransferOfferState.WITHDRAWN, responded_at=timezone.now()
            )
            breakdown.refresh_from_db()

        _reassign_trip(breakdown, vehicle)

        payload = {
            **breakdown.as_payload(),
            "offers": [o.as_payload() for o in breakdown.offers.all()],
        }
        notifications.transfer_accepted(breakdown, vehicle)
        broadcast_ops("transfer_accepted", payload)
        broadcast(vehicle_group(vehicle.callsign), "transfer_accepted", payload)
        broadcast(vehicle_group(breakdown.vehicle.callsign), "transfer_accepted", payload)
        hospital = breakdown.trip.destination_hospital
        if hospital:
            broadcast(hospital_group(hospital.code), "transfer_accepted", payload)
        return Response(payload)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        breakdown = self.get_object()
        vehicle = EmergencyVehicle.objects.filter(
            callsign=request.data.get("vehicle_callsign", "")
        ).first()
        offer = TransferOffer.objects.filter(breakdown=breakdown, vehicle=vehicle).first()
        if offer is None:
            return Response(
                {"detail": "This ambulance was not offered that transfer."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = RejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        offer.respond(
            TransferOfferState.REJECTED, request.user,
            serializer.validated_data.get("reason", ""),
        )
        broadcast_ops("transfer_rejected", offer.as_payload())
        return Response(offer.as_payload())


def _offer_transfer(breakdown) -> list[TransferOffer]:
    """Ask the nearest available crews, closest first."""
    candidates = (
        EmergencyVehicle.objects.dispatchable()
        .exclude(pk=breakdown.vehicle_id)
        .near(breakdown.latitude, breakdown.longitude, TRANSFER_SEARCH_RADIUS_M)
    )[:TRANSFER_MAX_OFFERS]

    offers = []
    for candidate in candidates:
        distance = getattr(candidate, "distance_m", None)
        if distance is None:
            distance = haversine_m(
                breakdown.latitude, breakdown.longitude,
                candidate.latitude, candidate.longitude,
            )
        offers.append(
            TransferOffer.objects.create(
                breakdown=breakdown, vehicle=candidate, distance_m=distance
            )
        )
    if not offers:
        log.error(
            "No ambulance within %.0f km could be offered breakdown %s",
            TRANSFER_SEARCH_RADIUS_M / 1000, breakdown.id,
        )
    return offers


def _push_offers(breakdown, offers) -> None:
    """Each offered crew hears about it on their own vehicle socket."""
    for offer in offers:
        broadcast(
            vehicle_group(offer.vehicle.callsign),
            "transfer_offer",
            {**offer.as_payload(), "breakdown": breakdown.as_payload()},
        )


def _reassign_trip(breakdown, replacement) -> None:
    """Move the patient's trip onto the replacement ambulance.

    The trip is reassigned rather than cancelled and recreated: its reference,
    clinical record, hospital notification and response timings all have to
    survive the vehicle changing underneath them, which is exactly what an
    audit of the incident will be reconstructed from.
    """
    from apps.dispatch.orchestrator import plan_route_to
    from apps.core.geo import Point

    trip = breakdown.trip
    trip.vehicle = replacement
    trip.save(update_fields=["vehicle", "updated_at"])

    replacement.status = VehicleStatus.TRANSPORTING
    replacement.save(update_fields=["status", "updated_at"])

    if trip.destination_latitude is not None:
        plan_route_to(
            trip,
            Point(trip.destination_latitude, trip.destination_longitude),
            reason=f"patient transfer from {breakdown.vehicle.callsign}",
        )
