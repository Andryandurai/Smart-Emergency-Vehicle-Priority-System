"""REST surface for crew takeover and the start-of-shift vehicle check.

The takeover is deliberately two-sided. A driver opening a shift names the
paramedic they are crewing with, and the shift stays ``pending`` until that
paramedic accepts it - so the roster records a pairing both people agreed to
rather than one person's claim about who else is on board. That is what makes
"who was on AMB-101 at 09:40" answerable after the fact.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core import notifications
from apps.core.enums import (
    EmergencyCategory,
    ShiftStatus,
    TripStage,
    VehicleStatus,
)
from apps.core.permissions import IsAmbulanceCrew, IsAuthenticatedRole, IsDriver
from apps.core.realtime import broadcast_ops
from apps.core.roles import Role
from apps.fleet.crew import EQUIPMENT_CATALOGUE, CrewShift, EquipmentCheck
from apps.fleet.models import EmergencyVehicle
from apps.fleet.readiness import apply_readiness

User = get_user_model()

#: How long after signing off a crew member reads "Shift Ended" rather than
#: "Off Duty" on the admin boards. One changeover's worth - long enough that a
#: supervisor watching a handover sees who has just come off, short enough
#: that yesterday's crew are plainly off duty.
RECENTLY_ENDED_HOURS = 8


def _person(user) -> dict | None:
    if user is None:
        return None
    full = user.get_full_name()
    return {"id": user.id, "username": user.get_username(), "name": full or user.get_username()}


class CrewShiftSerializer(serializers.ModelSerializer):
    vehicle_callsign = serializers.CharField(source="vehicle.callsign", read_only=True)
    vehicle_registration = serializers.CharField(source="vehicle.registration", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    driver_detail = serializers.SerializerMethodField()
    paramedic_detail = serializers.SerializerMethodField()
    equipment_check = serializers.SerializerMethodField()

    class Meta:
        model = CrewShift
        fields = [
            "id", "uuid", "vehicle", "vehicle_callsign", "vehicle_registration",
            "driver", "driver_detail", "paramedic", "paramedic_detail",
            "status", "status_display", "requested_at", "accepted_at", "ended_at",
            "decline_reason", "equipment_check",
        ]
        read_only_fields = ["uuid", "status", "accepted_at", "ended_at"]

    def get_driver_detail(self, obj):
        return _person(obj.driver)

    def get_paramedic_detail(self, obj):
        return _person(obj.paramedic)

    def get_equipment_check(self, obj):
        check = getattr(obj, "equipment_check", None)
        return check.as_payload() if check else None


class OpenShiftSerializer(serializers.Serializer):
    """A driver opening a takeover and naming their paramedic."""

    vehicle_callsign = serializers.CharField(max_length=32)
    paramedic_username = serializers.CharField(max_length=150)


class DeclineSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=300)


class EquipmentCheckSerializer(serializers.Serializer):
    """One submission of the checklist - partial submissions are allowed.

    A crew ticking items as they walk the vehicle should not lose the first
    fifteen because the sixteenth is missing, so each POST merges into what is
    already recorded rather than replacing it.
    """

    items = serializers.DictField(child=serializers.DictField(), required=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class SkipSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=300)


class CrewShiftViewSet(viewsets.ReadOnlyModelViewSet):
    """Shifts, takeover acceptance, and the vehicle check."""

    queryset = CrewShift.objects.select_related(
        "vehicle", "driver", "paramedic", "equipment_check"
    )
    serializer_class = CrewShiftSerializer
    permission_classes = [IsAuthenticatedRole]

    action_permissions = {
        # Driver-only: choosing the vehicle and opening the shift belong to
        # the person standing at it. A paramedic accepts a request; they do
        # not raise one.
        "open_shift": [IsDriver],
        "claim": [IsDriver],
        "request_paramedic": [IsDriver],
        "selectable_vehicles": [IsDriver],
        # Either seat.
        "accept": [IsAmbulanceCrew],
        "decline": [IsAmbulanceCrew],
        "end_shift": [IsAmbulanceCrew],
        "checklist": [IsAmbulanceCrew],
        "skip_checklist": [IsAmbulanceCrew],
        "new_emergency": [IsAmbulanceCrew],
    }

    def get_permissions(self):
        classes = self.action_permissions.get(self.action, self.permission_classes)
        return [cls() for cls in classes]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("open") == "1":
            qs = qs.open()
        if params.get("vehicle"):
            qs = qs.filter(vehicle__callsign=params["vehicle"])
        return qs

    # -- discovery ---------------------------------------------------------
    @action(detail=False, methods=["get"], url_path="crew")
    def crew_directory(self, request):
        """Who a driver can name as their paramedic.

        Paramedics only. Naming a hospital clerk - or another driver - as the
        attending clinician should not be possible, and a free-text username
        field invites exactly that.

        ``group_names_for`` rather than a bare group name, because the role
        carries legacy aliases and a member of an older group must still be
        findable.

        Falls back to the driver role only when nobody holds the paramedic
        role at all - a deployment upgraded but not yet re-seeded, where every
        crew member is still filed under the pre-split group. Once a single
        paramedic exists the fallback stops, so drivers do not linger in a
        picker that is asking for a clinician.
        """
        from apps.core.roles import group_names_for

        def members_of(role: str):
            return (
                User.objects.filter(
                    groups__name__in=group_names_for(role), is_active=True
                )
                .exclude(pk=request.user.pk)
                .order_by("first_name", "username")
                .distinct()
            )

        crew = list(members_of(Role.PARAMEDIC))
        if not crew:
            crew = list(members_of(Role.AMBULANCE))
        return Response({"crew": [_person(u) for u in crew]})

    @action(detail=False, methods=["get"], url_path="equipment-catalogue")
    def equipment_catalogue(self, request):
        return Response({"items": EQUIPMENT_CATALOGUE})

    @action(detail=False, methods=["get"], url_path="selectable-vehicles")
    def selectable_vehicles(self, request):
        """Ambulances a driver may take over right now.

        Available, uncrewed and not grounded. Anything failing one of those is
        excluded here rather than shown greyed out - a picker that offers a
        vehicle the server will refuse is a picker that wastes a driver's time
        at the start of a shift.
        """
        vehicles = (
            EmergencyVehicle.objects.selectable_for_takeover()
            .prefetch_related("shifts")
            .order_by("callsign")
        )
        return Response(
            {
                "vehicles": [
                    {
                        **vehicle.as_tracking_payload(),
                        "home_station": (
                            vehicle.home_station.name if vehicle.home_station else None
                        ),
                        "last_inspected_at": vehicle.readiness_updated_at,
                    }
                    for vehicle in vehicles
                ]
            }
        )

    @action(
        detail=False, methods=["get"], url_path="roster",
        permission_classes=[IsAuthenticatedRole],
    )
    def roster(self, request):
        """Every driver and paramedic, with whatever they are currently doing.

        The control room's crew board. Assembled from the shift rather than
        from the account, because "who is crewing AMB-104 right now" is a
        property of the shift and nothing else - an account tells you a person
        exists, not that they are on the road with somebody.

        One endpoint for both seats so the two admin tabs cannot drift into
        showing different versions of the same pairing.
        """
        from apps.core.profiles import StaffProfile
        from apps.core.roles import group_names_for

        drivers = User.objects.filter(
            groups__name__in=group_names_for(Role.AMBULANCE), is_active=True
        ).distinct()
        paramedics = User.objects.filter(
            groups__name__in=group_names_for(Role.PARAMEDIC), is_active=True
        ).distinct()

        open_shifts = list(
            CrewShift.objects.open().select_related(
                "vehicle", "driver", "paramedic", "equipment_check"
            )
        )
        live = {shift.driver_id: shift for shift in open_shifts}
        by_paramedic = {
            shift.paramedic_id: shift for shift in open_shifts if shift.paramedic_id
        }

        # The shift somebody has just come off, so the board can say "Shift
        # ended" for the hour after they sign off rather than jumping straight
        # to "Off duty" - the two mean different things to a supervisor
        # looking at the roster mid-changeover. Ascending, so the last write
        # per person is their most recent shift.
        since = timezone.now() - timezone.timedelta(hours=RECENTLY_ENDED_HOURS)
        ended_driver: dict[int, CrewShift] = {}
        ended_paramedic: dict[int, CrewShift] = {}
        for shift in CrewShift.objects.filter(
            status__in=[ShiftStatus.ENDED, ShiftStatus.DECLINED], ended_at__gte=since
        ).order_by("ended_at"):
            ended_driver[shift.driver_id] = shift
            if shift.paramedic_id:
                ended_paramedic[shift.paramedic_id] = shift

        profiles = {p.user_id: p for p in StaffProfile.objects.all()}

        return Response(
            {
                "generated_at": timezone.now(),
                "drivers": [
                    self._crew_row(
                        user, live.get(user.id), "driver", profiles, request,
                        ended=ended_driver.get(user.id),
                    )
                    for user in drivers.order_by("first_name", "username")
                ],
                "paramedics": [
                    self._crew_row(
                        user, by_paramedic.get(user.id), "paramedic", profiles, request,
                        ended=ended_paramedic.get(user.id),
                    )
                    for user in paramedics.order_by("first_name", "username")
                ],
            }
        )

    @staticmethod
    def _crew_row(user, shift, seat: str, profiles: dict, request, ended=None) -> dict:
        """One person, their pairing, and the job they are on."""
        profile = profiles.get(user.id)
        vehicle = shift.vehicle if shift else None
        trip = vehicle.active_trip if vehicle else None
        partner = None
        if shift:
            partner = shift.paramedic if seat == "driver" else shift.driver

        check = getattr(shift, "equipment_check", None) if shift else None

        return {
            "id": user.id,
            "username": user.get_username(),
            "name": user.get_full_name() or user.get_username(),
            "email": user.email,
            "staff_id": profile.staff_id if profile else "",
            "qualification": profile.qualification if profile else "",
            "base_station": profile.base_station if profile else "",
            "phone": profile.phone if profile else "",
            "blood_group": profile.blood_group if profile else "",
            "avatar_url": (
                request.build_absolute_uri(profile.avatar.url)
                if profile and profile.avatar
                else None
            ),
            # --- assignment ---
            "on_duty": bool(shift and shift.status == ShiftStatus.ACTIVE),
            # No open shift of any kind. Distinct from ``on_duty``, which is
            # false for a driver mid-takeover as well - somebody standing at
            # an ambulance running its inspection is not off duty, and the
            # Off Duty board must not claim they are.
            "off_duty": shift is None,
            "shift_status": (
                shift.get_status_display() if shift
                else "Shift ended" if ended is not None
                else "Off duty"
            ),
            "vehicle": vehicle.callsign if vehicle else None,
            "vehicle_registration": vehicle.registration if vehicle else "",
            "partner": (partner.get_full_name() or partner.get_username()) if partner else None,
            "on_duty_since": shift.accepted_at if shift else None,
            # --- the job ---
            "mission": trip.reference if trip else None,
            "mission_category": trip.get_emergency_category_display() if trip else None,
            "mission_stage": trip.get_stage_display() if trip else None,
            "mission_hospital": (
                trip.destination_hospital.name
                if trip and trip.destination_hospital_id
                else None
            ),
            "mission_eta": trip.eta if trip else None,
            "mission_priority": trip.priority_level if trip else None,
            # --- live monitoring ---
            "status": CrewShiftViewSet._live_status(shift, vehicle, trip, seat, ended),
            "monitoring": {
                "latitude": vehicle.latitude if vehicle else None,
                "longitude": vehicle.longitude if vehicle else None,
                "speed_kmh": vehicle.speed_kmh if vehicle else None,
                "heading_deg": vehicle.heading_deg if vehicle else None,
                "last_seen_at": vehicle.last_seen_at if vehicle else None,
                "is_stale": vehicle.is_stale if vehicle else None,
                "vehicle_readiness": vehicle.get_readiness_display() if vehicle else None,
                "inspection": (
                    "Complete" if check and check.is_complete
                    else "Skipped - outstanding" if check and check.skipped
                    else "Outstanding" if check
                    else None
                ),
                "distance_remaining_m": trip.distance_remaining_m if trip else None,
            },
        }

    @staticmethod
    def _live_status(shift, vehicle, trip, seat: str, ended) -> str:
        """Where this person is in their shift, right now.

        One vocabulary for both crew boards, derived from the shift and the
        trip rather than stored anywhere, so it cannot go stale: the moment a
        trip advances a stage or a shift ends, the next read of the roster
        says something different without anybody updating a status field.

        Deliberately not ``trip.get_stage_display()``. The stage labels are
        written for a dispatcher reading one response ("En route to scene",
        "On scene - patient assessment"); a supervisor scanning forty crew
        cards needs two words per person, in the same words the driver and
        paramedic portals use.
        """
        if shift is None:
            # Just come off a shift, rather than never on one today.
            return "Shift Ended" if ended is not None else "Off Duty"

        if shift.status in {ShiftStatus.DRAFT, ShiftStatus.PENDING}:
            # Vehicle claimed, inspection running, or waiting on the paramedic
            # to accept. Signed on, not yet crewed.
            return "On Duty"

        if trip is not None:
            if trip.stage in {TripStage.CREATED, TripStage.TO_SCENE}:
                return "On Route"
            if trip.stage == TripStage.ON_SCENE:
                # A paramedic on scene with an assessed patient is treating
                # them; the driver alongside is not. Same trip, two jobs.
                treating = seat == "paramedic" and (
                    trip.emergency_category != EmergencyCategory.UNKNOWN
                    or trip.destination_hospital_id is not None
                )
                return "Treating Patient" if treating else "On Scene"
            if trip.stage == TripStage.TO_HOSPITAL:
                return "Transporting"
            if trip.stage == TripStage.ARRIVED:
                return "At Hospital"
            return trip.get_stage_display()

        # Active, no job. "Available" only when the ambulance itself is free -
        # a crew whose vehicle is still returning to base or parked at a
        # hospital bay is on duty but not yet takeable by dispatch.
        if vehicle is not None and vehicle.status == VehicleStatus.AVAILABLE:
            return "Available"
        return "On Duty"

    @action(detail=False, methods=["get"], url_path="mine")
    def mine(self, request):
        """The signed-in user's open shift, plus takeovers awaiting them.

        One call because the paramedic screen needs both to decide what to
        render, and two round-trips would let it show a stale half-state.
        """
        open_shifts = CrewShift.objects.open().for_user(request.user).select_related(
            "vehicle", "driver", "paramedic", "equipment_check"
        )
        mine = next((s for s in open_shifts), None)
        awaiting = [
            s for s in open_shifts
            if s.status == ShiftStatus.PENDING and s.paramedic_id == request.user.id
        ]
        return Response(
            {
                "shift": CrewShiftSerializer(mine).data if mine else None,
                "awaiting_my_acceptance": CrewShiftSerializer(awaiting, many=True).data,
                "role_hint": "driver" if mine and mine.driver_id == request.user.id else "paramedic",
                # Whether a skipped inspection has come due. Served from here
                # so the console never has to re-derive the rule that
                # `new_emergency` enforces - two implementations of "may this
                # crew take another patient" is one too many.
                "checklist_due": self._outstanding_skip(mine) if mine else None,
            }
        )

    # -- takeover ----------------------------------------------------------
    @action(detail=False, methods=["post"])
    def claim(self, request):
        """Step 1: the driver takes the vehicle, before naming anyone.

        Creates a DRAFT shift so the readiness inspection has something to be
        recorded against, and so the vehicle is reserved while the driver
        walks it. Nobody is asked to crew yet - a paramedic called to an
        ambulance that turns out to have failed brakes is the one person the
        driver most needs still available.
        """
        callsign = request.data.get("vehicle_callsign", "")
        vehicle = EmergencyVehicle.objects.filter(callsign=callsign).first()
        if vehicle is None:
            return Response({"detail": "Unknown vehicle."}, status=status.HTTP_404_NOT_FOUND)

        try:
            with transaction.atomic():
                shift = CrewShift.objects.create(
                    vehicle=vehicle, driver=request.user, status=ShiftStatus.DRAFT
                )
                EquipmentCheck.objects.create(shift=shift)
        except IntegrityError:
            existing = CrewShift.objects.open().filter(vehicle=vehicle).first()
            return Response(
                {
                    "detail": f"{vehicle.callsign} has already been taken.",
                    "shift": CrewShiftSerializer(existing).data if existing else None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        payload = CrewShiftSerializer(shift).data
        broadcast_ops("shift_claimed", payload)
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="request-paramedic")
    def request_paramedic(self, request, pk=None):
        """Step 3: having inspected the vehicle, ask a paramedic to crew it."""
        shift = self.get_object()
        if shift.driver_id != request.user.id:
            return Response(
                {"detail": "Only this shift's driver can send the sync request."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if shift.status not in {ShiftStatus.DRAFT, ShiftStatus.PENDING}:
            return Response(
                {"detail": f"This shift is already {shift.get_status_display().lower()}."},
                status=status.HTTP_409_CONFLICT,
            )

        # A grounded vehicle must not have a colleague summoned to it.
        check = getattr(shift, "equipment_check", None)
        if check is not None and check.missing_critical:
            return Response(
                {
                    "detail": (
                        f"{shift.vehicle.callsign} failed its readiness check and cannot "
                        f"be crewed. Clear the fault or take another ambulance."
                    ),
                    "missing_critical": check.missing_critical,
                },
                status=status.HTTP_409_CONFLICT,
            )

        username = request.data.get("paramedic_username", "")
        paramedic = User.objects.filter(username=username, is_active=True).first()
        if paramedic is None:
            return Response({"detail": "Unknown paramedic."}, status=status.HTTP_404_NOT_FOUND)
        if paramedic.id == request.user.id:
            return Response(
                {"detail": "The driver and the paramedic must be two different people."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        shift.paramedic = paramedic
        shift.status = ShiftStatus.PENDING
        shift.requested_at = timezone.now()
        shift.save(update_fields=["paramedic", "status", "requested_at", "updated_at"])

        payload = CrewShiftSerializer(shift).data
        broadcast_ops("shift_requested", payload)
        return Response(payload)

    @action(detail=False, methods=["post"], url_path="open")
    def open_shift(self, request):
        """Claim and request in one call.

        Retained unchanged for callers that predate the split into
        claim / inspect / request - notably the existing tests and any field
        device already using it. New clients should use ``claim`` and
        ``request-paramedic`` so the inspection happens in between.
        """
        serializer = OpenShiftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        vehicle = EmergencyVehicle.objects.filter(callsign=data["vehicle_callsign"]).first()
        if vehicle is None:
            return Response({"detail": "Unknown vehicle."}, status=status.HTTP_404_NOT_FOUND)

        paramedic = User.objects.filter(username=data["paramedic_username"], is_active=True).first()
        if paramedic is None:
            return Response({"detail": "Unknown paramedic."}, status=status.HTTP_404_NOT_FOUND)
        if paramedic.id == request.user.id:
            return Response(
                {"detail": "The driver and the paramedic must be two different people."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            with transaction.atomic():
                shift = CrewShift.objects.create(
                    vehicle=vehicle, driver=request.user, paramedic=paramedic,
                    status=ShiftStatus.PENDING,
                )
                EquipmentCheck.objects.create(shift=shift)
        except IntegrityError:
            # The unique constraint - somebody already has this vehicle.
            existing = CrewShift.objects.open().filter(vehicle=vehicle).first()
            return Response(
                {
                    "detail": f"{vehicle.callsign} already has an open shift.",
                    "shift": CrewShiftSerializer(existing).data if existing else None,
                },
                status=status.HTTP_409_CONFLICT,
            )

        payload = CrewShiftSerializer(shift).data
        broadcast_ops("shift_requested", payload)
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        """Paramedic accepts the takeover. Only the named paramedic may."""
        shift = self.get_object()
        if shift.paramedic_id != request.user.id:
            return Response(
                {"detail": "Only the paramedic named on this takeover can accept it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if shift.status != ShiftStatus.PENDING:
            return Response(
                {"detail": f"This takeover is already {shift.get_status_display().lower()}."},
                status=status.HTTP_409_CONFLICT,
            )
        shift.accept()
        payload = CrewShiftSerializer(shift).data
        broadcast_ops("shift_accepted", payload)
        return Response(payload)

    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        shift = self.get_object()
        if shift.paramedic_id != request.user.id:
            return Response(
                {"detail": "Only the paramedic named on this takeover can decline it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = DeclineSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        shift.decline(serializer.validated_data.get("reason", ""))
        return Response(CrewShiftSerializer(shift).data)

    @action(detail=True, methods=["post"], url_path="end")
    def end_shift(self, request, pk=None):
        shift = self.get_object()
        if request.user.id not in {shift.driver_id, shift.paramedic_id}:
            return Response(
                {"detail": "Only this shift's crew can end it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        shift.end()
        payload = CrewShiftSerializer(shift).data
        broadcast_ops("shift_ended", payload)
        return Response(payload)

    @staticmethod
    def _outstanding_skip(shift) -> dict | None:
        """The refusal payload when a skipped check has come due, else None.

        Due means: the inspection was skipped, is still not finished, and the
        response it was skipped for has since ended. Checking that a trip
        actually completed - rather than simply that a skip exists - is what
        keeps the emergency skip usable: the crew who skipped it are still
        allowed to run the call they skipped it for.
        """
        from apps.dispatch.models import EmergencyTrip

        check = getattr(shift, "equipment_check", None)
        if check is None or not check.skipped or not check.is_outstanding:
            return None

        finished = EmergencyTrip.objects.filter(
            vehicle=shift.vehicle,
            stage__in=[TripStage.HANDOVER, TripStage.CANCELLED],
        )
        if check.skipped_at:
            finished = finished.filter(updated_at__gte=check.skipped_at)
        if not finished.exists():
            return None

        return {
            "detail": (
                f"{shift.vehicle.callsign} still owes its vehicle readiness check. "
                f"It was skipped for the last emergency "
                f"(“{check.skip_reason}”) and must be completed before "
                f"another patient is taken."
            ),
            "checklist_outstanding": True,
            "answered": check.answered_count,
            "total": len(EQUIPMENT_CATALOGUE),
            "skip_reason": check.skip_reason,
            "skipped_at": check.skipped_at,
        }

    @action(detail=True, methods=["post"], url_path="new-emergency")
    def new_emergency(self, request, pk=None):
        """Open a response for this crew's own ambulance.

        The paramedic's "New Emergency". Dispatch normally opens responses -
        a call comes in, the centre assigns a vehicle - but a crew flagged
        down at the roadside has a patient and no dispatch record, and
        refusing to let them start one means the transport happens with no
        route, no corridor and no hospital pre-alert.

        Deliberately narrower than ``/dispatch/trips/``: it takes no vehicle
        parameter at all. The vehicle is the one this shift is signed on to,
        so a crew can only ever open a response for the ambulance they are
        actually sitting in.
        """
        shift = self.get_object()
        if request.user.id not in {shift.driver_id, shift.paramedic_id}:
            return Response(
                {"detail": "Only this shift's crew can open a response."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if shift.status != ShiftStatus.ACTIVE:
            return Response(
                {"detail": "The shift is not active."},
                status=status.HTTP_409_CONFLICT,
            )

        from apps.dispatch.models import EmergencyTrip
        from apps.dispatch.serializers import EmergencyTripSerializer

        vehicle = shift.vehicle
        existing = vehicle.active_trip

        # The skipped inspection comes due once the emergency it was skipped
        # for is over.
        #
        # An emergency skip buys exactly one response - that is its whole
        # purpose, and refusing that response would make the skip pointless.
        # What it must not buy is every response after it: an ambulance whose
        # oxygen and defibrillator were never checked is a vehicle nobody has
        # confirmed can treat the *next* patient, and "we will do it later"
        # has no later if nothing ever asks. So the debt is called in at the
        # only safe moment - between patients, with the vehicle empty.
        if existing is None:
            outstanding = self._outstanding_skip(shift)
            if outstanding is not None:
                return Response(outstanding, status=status.HTTP_409_CONFLICT)
        if existing is not None:
            # An emergency that is already *under way* - patient aboard,
            # hospital assigned, transport running - blocks a second one. Two
            # live trips on one ambulance means two ETAs and two hospitals
            # expecting the same patient. The way out is to finish the
            # handover or cancel, both explicit acts.
            #
            # A trip that is merely open (dispatched, en route to scene, on
            # scene with no destination yet) is *this* emergency waiting to be
            # filled in, and is returned so the screen can record against it.
            if existing.destination_hospital_id and existing.stage in {
                TripStage.TO_HOSPITAL, TripStage.ARRIVED
            }:
                return Response(
                    {
                        "detail": (
                            f"{vehicle.callsign} is already transporting a patient on "
                            f"{existing.reference} to {existing.destination_hospital.name}. "
                            f"Complete the handover or cancel it before starting another "
                            f"emergency."
                        ),
                        "active_trip": EmergencyTripSerializer(
                            existing, context={"request": request}
                        ).data,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            return Response(
                EmergencyTripSerializer(existing, context={"request": request}).data
            )

        trip = EmergencyTrip.objects.create(
            vehicle=vehicle,
            emergency_category=EmergencyCategory.UNKNOWN,
            stage=TripStage.ON_SCENE,
            incident_latitude=vehicle.latitude,
            incident_longitude=vehicle.longitude,
            incident_address=request.data.get("incident_address", ""),
            arrived_scene_at=timezone.now(),
        )
        vehicle.status = VehicleStatus.ON_SCENE
        vehicle.save(update_fields=["status", "updated_at"])

        payload = EmergencyTripSerializer(trip, context={"request": request}).data
        broadcast_ops("trip_created", {"reference": trip.reference, "vehicle": vehicle.callsign})
        return Response(payload, status=status.HTTP_201_CREATED)

    # -- daily vehicle check ------------------------------------------------
    @action(detail=True, methods=["post"])
    def checklist(self, request, pk=None):
        """Record checklist answers. Merges, so partial progress is kept."""
        shift = self.get_object()
        if request.user.id not in {shift.driver_id, shift.paramedic_id}:
            return Response(
                {"detail": "Only this shift's crew can complete its vehicle check."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = EquipmentCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        check, _ = EquipmentCheck.objects.get_or_create(shift=shift)
        merged = dict(check.items or {})
        merged.update(data.get("items") or {})
        check.items = merged
        if "notes" in data:
            check.notes = data["notes"]

        if check.is_complete:
            check.completed_at = timezone.now()
            check.completed_by = request.user
            # Completing clears the outstanding skip - the check has now
            # actually been done, which is the whole point of allowing it to
            # be deferred rather than waived.
            check.skipped = False
        check.save()

        # Readiness follows from the answers, never from a driver asserting
        # it - see EquipmentCheck.derived_readiness.
        outcome = apply_readiness(check, actor=request.user)
        return Response({**check.as_payload(), **outcome})

    @action(detail=True, methods=["post"], url_path="checklist/skip")
    def skip_checklist(self, request, pk=None):
        """Emergency skip - go now, complete the check later.

        Not a waiver. The check stays outstanding and the reason is attributed,
        because a vehicle that went out unchecked is a fact somebody may need
        to explain later.
        """
        shift = self.get_object()
        if request.user.id not in {shift.driver_id, shift.paramedic_id}:
            return Response(
                {"detail": "Only this shift's crew can skip its vehicle check."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = SkipSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Only before the vehicle is committed to a call. Once a crew has
        # accepted a dispatch the skip has no purpose - the emergency it
        # exists for has already started - and offering it then would just be
        # a way to defer an inspection indefinitely.
        if shift.vehicle.active_trip is not None:
            return Response(
                {
                    "detail": (
                        "This ambulance is already on a call. The inspection can be "
                        "completed once the emergency ends."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        check, _ = EquipmentCheck.objects.get_or_create(shift=shift)
        check.skipped = True
        check.skip_reason = serializer.validated_data["reason"]
        check.skipped_at = timezone.now()
        check.save(update_fields=["skipped", "skip_reason", "skipped_at", "updated_at"])

        outcome = apply_readiness(check, actor=request.user)
        notifications.inspection_skipped(shift.vehicle, shift, check.skip_reason)

        payload = {**check.as_payload(), **outcome}
        broadcast_ops("equipment_check_skipped", {"shift_id": shift.id, **payload})
        return Response(payload)
