"""The receiving hospital's own console (Layer 5, hospital side).

Everything a charge nurse needs on one screen, assembled from state the
platform already holds rather than from a parallel hospital record: the trips
bound here, the crew on each ambulance, the corridor's ETA, and the ward's own
capacity and roster.

Three endpoints, matching the three tabs:

``dashboard``   the board - status, counts, teams, beds
``ambulances``  inbound units in the order they will actually arrive
``update``      the ward's edits to capacity and team readiness

The split matters for refresh rates. The board and the ambulance list are
polled continuously by an unattended wall display; the update endpoint is
written by hand a few times a shift. Serving them from one fat endpoint would
mean every poll re-serialised editable fields nobody was looking at.
"""
from __future__ import annotations

from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.core.enums import BreakdownState, TripStage, VehicleStatus
from apps.core.permissions import IsAuthenticatedRole, IsHospitalStaff
from apps.core.realtime import broadcast, broadcast_ops, hospital_group
from apps.core.roles import Role, has_role
from apps.hospitals.models import Hospital, HospitalCapacity, HospitalTeamReadiness

#: Stages that mean "on the way here". ARRIVED is included deliberately: the
#: ambulance is on the forecourt and the patient has not been handed over, which
#: is precisely when the ED most needs the row on screen.
INBOUND_STAGES = (TripStage.TO_SCENE, TripStage.ON_SCENE, TripStage.TO_HOSPITAL, TripStage.ARRIVED)


def resolve_hospital(request, code: str | None = None) -> Hospital | None:
    """Which hospital is this request about?

    Tenancy first: when a deployment has bound a hospital to a staff group,
    that binding wins and an explicit code cannot be used to look at somebody
    else's ward. The pilot leaves ``staff_group`` unset - there is no tenancy
    to enforce - so the caller may name a hospital, and failing that gets the
    first active one. Administrators may always name one; that is how the
    control room inspects a hospital's board.
    """
    if code:
        chosen = Hospital.objects.filter(code__iexact=code, is_active=True).first()
    else:
        chosen = None

    user = request.user
    if user.is_authenticated and not has_role(user, Role.ADMIN):
        bound = Hospital.objects.filter(
            staff_group__in=user.groups.all(), is_active=True
        ).first()
        if bound is not None:
            return bound

    return chosen or Hospital.objects.filter(is_active=True).order_by("name").first()


# ---------------------------------------------------------------------------
# Serializers for the editable surfaces
# ---------------------------------------------------------------------------
class CapacityUpdateSerializer(serializers.ModelSerializer):
    """Exactly the figures the ward may edit from the Updates tab."""

    class Meta:
        model = HospitalCapacity
        fields = [
            "emergency_cases_today",
            "emergency_beds_total", "emergency_beds_available",
            "icu_beds_total", "icu_beds_available",
            "general_beds_total", "general_beds_available",
            "pediatric_beds_total", "pediatric_beds_available",
            "burn_unit_beds_total", "burn_unit_beds_available",
            "cardiac_icu_total", "cardiac_icu_available",
            "ventilators_total", "ventilators_available",
            "operation_theatres_total", "operation_theatres_free",
            "emergency_staff_on_duty", "doctors_on_duty", "patients_waiting",
        ]

    def validate(self, attrs):
        """Available may never exceed total.

        Checked against the merged record rather than the payload, because the
        Updates tab sends one field at a time: a lone "ICU available = 9" is
        only wrong in the light of the total already stored.
        """
        merged = {**{f: getattr(self.instance, f) for f in self.Meta.fields}, **attrs}
        pairs = [
            ("emergency_beds_available", "emergency_beds_total", "Emergency beds"),
            ("icu_beds_available", "icu_beds_total", "ICU beds"),
            ("general_beds_available", "general_beds_total", "General beds"),
            ("pediatric_beds_available", "pediatric_beds_total", "Pediatric beds"),
            ("burn_unit_beds_available", "burn_unit_beds_total", "Burn unit beds"),
            ("cardiac_icu_available", "cardiac_icu_total", "Cardiac ICU beds"),
            ("ventilators_available", "ventilators_total", "Ventilators"),
            ("operation_theatres_free", "operation_theatres_total", "Operation theatres"),
        ]
        errors = {}
        for available, total, label in pairs:
            if merged[available] > merged[total]:
                errors[available] = (
                    f"{label}: {merged[available]} available cannot exceed {merged[total]} total."
                )
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class ReadinessUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = HospitalTeamReadiness
        fields = [field for field, _ in HospitalTeamReadiness.TEAMS]


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------
def _bed_rows(capacity: HospitalCapacity) -> list[dict]:
    """The ward table, in the order the hospital board lists it."""
    return [
        {"key": "general_beds", "label": "General Beds",
         "available": capacity.general_beds_available, "total": capacity.general_beds_total},
        {"key": "icu_beds", "label": "ICU Beds",
         "available": capacity.icu_beds_available, "total": capacity.icu_beds_total},
        {"key": "emergency_beds", "label": "Emergency Beds",
         "available": capacity.emergency_beds_available, "total": capacity.emergency_beds_total},
        {"key": "pediatric_beds", "label": "Pediatric Beds",
         "available": capacity.pediatric_beds_available, "total": capacity.pediatric_beds_total},
        {"key": "burn_unit", "label": "Burn Unit",
         "available": capacity.burn_unit_beds_available, "total": capacity.burn_unit_beds_total},
        {"key": "cardiac_icu", "label": "Cardiac ICU",
         "available": capacity.cardiac_icu_available, "total": capacity.cardiac_icu_total},
        {"key": "ventilators", "label": "Ventilators",
         "available": capacity.ventilators_available, "total": capacity.ventilators_total},
    ]


def dashboard_payload(hospital: Hospital) -> dict:
    from apps.dispatch.models import EmergencyTrip

    capacity = hospital.capacity
    readiness = hospital.readiness
    inbound = EmergencyTrip.objects.filter(
        destination_hospital=hospital, stage__in=INBOUND_STAGES
    ).count()

    return {
        "hospital": {
            "id": hospital.id,
            "code": hospital.code,
            "name": hospital.name,
            "city": hospital.city,
            "emergency_phone": hospital.emergency_phone,
            "is_trauma_designated": hospital.is_trauma_designated,
            "is_on_diversion": hospital.is_on_diversion,
            "diversion_reason": hospital.diversion_reason,
            "latitude": hospital.latitude,
            "longitude": hospital.longitude,
        },
        "status": capacity.status,
        "active_ambulances_coming": inbound,
        "emergency_cases_today": capacity.emergency_cases_today,
        "available_beds": capacity.emergency_beds_available,
        "available_icu_beds": capacity.icu_beds_available,
        "available_ventilators": capacity.ventilators_available,
        "available_operation_theatres": capacity.operation_theatres_free,
        "emergency_staff_on_duty": capacity.emergency_staff_on_duty,
        "teams": readiness.as_rows(),
        "teams_ready": readiness.ready_count,
        "teams_total": len(HospitalTeamReadiness.TEAMS),
        "beds": _bed_rows(capacity),
        "workload_index": capacity.workload_index,
        "patients_waiting": capacity.patients_waiting,
        "doctors_on_duty": capacity.doctors_on_duty,
        "reported_at": capacity.reported_at,
        "is_stale": capacity.is_stale,
    }


def _crew_for(vehicle) -> dict:
    """Who is on this ambulance, from the live shift."""
    from apps.fleet.crew import CrewShift

    shift = (
        CrewShift.objects.live()
        .filter(vehicle=vehicle)
        .select_related("driver", "paramedic")
        .first()
    )
    if shift is None:
        return {"driver_name": None, "paramedic_name": None}

    def name(user):
        if user is None:
            return None
        return user.get_full_name() or user.get_username()

    return {"driver_name": name(shift.driver), "paramedic_name": name(shift.paramedic)}


def _ambulance_row(trip, request) -> dict:
    vehicle = trip.vehicle
    route = trip.active_route
    return {
        "trip_id": trip.id,
        "reference": trip.reference,
        "ambulance_number": vehicle.callsign,
        "registration": vehicle.registration,
        **_crew_for(vehicle),
        "current_location": {
            "latitude": vehicle.latitude,
            "longitude": vehicle.longitude,
            "heading_deg": vehicle.heading_deg,
            "speed_kmh": vehicle.speed_kmh,
        },
        "eta": trip.eta,
        "distance_remaining_m": trip.distance_remaining_m,
        "current_status": trip.get_stage_display(),
        "stage": trip.stage,
        "vehicle_status": vehicle.get_status_display(),
        "emergency_level": trip.priority_level,
        "patient_category": trip.get_emergency_category_display(),
        "emergency_category": trip.emergency_category,
        # Clinical detail. Withheld from any role without clearance by the same
        # rule the trip serializer applies - a hospital holds it, a traffic
        # controller must not.
        "patient": _patient_block(trip, request),
        "route_geometry": (route.geometry if route else []),
        "destination": {
            "latitude": trip.destination_latitude,
            "longitude": trip.destination_longitude,
        },
        "has_arrived": trip.stage == TripStage.ARRIVED,
    }


def _patient_block(trip, request) -> dict:
    from apps.core.roles import may_view_clinical_data
    from apps.core.enums import PatientSymptom

    if not may_view_clinical_data(getattr(request, "user", None)):
        return {"redacted": True}

    selected = set(trip.symptoms or [])
    return {
        "redacted": False,
        "emergency_category": trip.get_emergency_category_display(),
        "assessment": trip.patient_notes or "",
        "symptoms": [label for value, label in PatientSymptom.choices if value in selected],
        "priority_level": trip.priority_level,
        "patient_age": trip.patient_age,
        "deteriorating": trip.patient_deteriorating,
        "eta": trip.eta,
    }


def _breakdowns_for(hospital) -> list[dict]:
    """Open breakdowns on ambulances that were bringing a patient here.

    A crew stranded with a patient bound for this ED is the hospital's problem
    too - the bed they were holding is now needed later, or not at all - so it
    belongs on their board rather than only on the control room's.
    """
    from apps.fleet.maintenance import BreakdownEvent

    events = (
        BreakdownEvent.objects.filter(
            state__in=[BreakdownState.OPEN, BreakdownState.TRANSFER_ACCEPTED],
            trip__destination_hospital=hospital,
        )
        .select_related("vehicle", "trip", "replacement_vehicle")
        .order_by("-created_at")
    )
    return [event.as_payload() for event in events]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def hospital_dashboard(request):
    """Tab 1 - the board."""
    hospital = resolve_hospital(request, request.query_params.get("hospital"))
    if hospital is None:
        return Response({"detail": "No active hospital."}, status=status.HTTP_404_NOT_FOUND)
    return Response(dashboard_payload(hospital))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def hospital_ambulances(request):
    """Tab 2 - inbound units, in the order they will arrive.

    Ordered by priority and then by ETA, which is the order a receiving team
    actually works in: a Level 1 twelve minutes out is prepared for before a
    Level 3 that arrives sooner. Rows with no ETA sort last rather than first -
    an unknown arrival is not an imminent one.
    """
    hospital = resolve_hospital(request, request.query_params.get("hospital"))
    if hospital is None:
        return Response({"detail": "No active hospital."}, status=status.HTTP_404_NOT_FOUND)

    from apps.dispatch.models import EmergencyTrip

    trips = (
        EmergencyTrip.objects.filter(destination_hospital=hospital, stage__in=INBOUND_STAGES)
        .select_related("vehicle", "destination_hospital")
        .prefetch_related("routes")
    )
    rows = [_ambulance_row(trip, request) for trip in trips]
    far_future = timezone.now() + timezone.timedelta(days=365)
    rows.sort(key=lambda r: (r["emergency_level"], r["eta"] or far_future))

    return Response(
        {
            "hospital": {"id": hospital.id, "code": hospital.code, "name": hospital.name},
            "count": len(rows),
            "ambulances": rows,
            "breakdowns": _breakdowns_for(hospital),
        }
    )


@api_view(["POST"])
@permission_classes([IsHospitalStaff])
def patient_received(request, trip_id: int):
    """The receiving team confirms the patient is with them.

    This is the hospital's half of the handover. The crew's own console has a
    handover button too, and both end at ``advance_stage(HANDOVER)`` - the
    difference is only who pressed it, which is recorded in the reason so a
    later review can tell an ED confirming receipt from a crew closing their
    own job.
    """
    from apps.dispatch import orchestrator
    from apps.dispatch.models import EmergencyTrip
    from apps.dispatch.serializers import EmergencyTripSerializer

    trip = EmergencyTrip.objects.filter(pk=trip_id).select_related(
        "vehicle", "destination_hospital"
    ).first()
    if trip is None:
        return Response({"detail": "Unknown trip."}, status=status.HTTP_404_NOT_FOUND)

    hospital = resolve_hospital(request, request.data.get("hospital"))
    if hospital and trip.destination_hospital_id != hospital.id:
        return Response(
            {"detail": "That patient is not being brought to this hospital."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if trip.stage in {TripStage.HANDOVER, TripStage.CANCELLED}:
        return Response(
            {"detail": f"This response is already {trip.get_stage_display().lower()}."},
            status=status.HTTP_409_CONFLICT,
        )

    who = request.user.get_username() if request.user.is_authenticated else "hospital"
    orchestrator.advance_stage(
        trip, TripStage.HANDOVER, reason=f"patient received by {who}"
    )
    trip.vehicle.status = VehicleStatus.AVAILABLE
    trip.vehicle.save(update_fields=["status", "updated_at"])

    # One more emergency treated today. Counted here rather than on arrival so
    # the tally means "patients taken in", not "ambulances that turned up".
    if trip.destination_hospital_id:
        capacity = trip.destination_hospital.capacity
        capacity.emergency_cases_today += 1
        capacity.save(update_fields=["emergency_cases_today", "updated_at"])

    payload = EmergencyTripSerializer(trip, context={"request": request}).data
    broadcast_ops("patient_received", payload)
    if trip.destination_hospital_id:
        broadcast(hospital_group(trip.destination_hospital.code), "patient_received", payload)
    return Response(payload)


@api_view(["GET", "PATCH"])
@permission_classes([IsHospitalStaff])
def hospital_update(request):
    """Tab 3 - read and write the ward's own figures and roster.

    One endpoint for both halves because they are edited together on one
    screen, and a partial save that updated the beds but not the teams would
    leave the board describing two different moments.
    """
    hospital = resolve_hospital(request, request.query_params.get("hospital") or request.data.get("hospital"))
    if hospital is None:
        return Response({"detail": "No active hospital."}, status=status.HTTP_404_NOT_FOUND)

    capacity, _ = HospitalCapacity.objects.get_or_create(hospital=hospital)
    readiness, _ = HospitalTeamReadiness.objects.get_or_create(hospital=hospital)

    if request.method == "GET":
        return Response(
            {
                "hospital": {"id": hospital.id, "code": hospital.code, "name": hospital.name},
                "capacity": CapacityUpdateSerializer(capacity).data,
                "teams": ReadinessUpdateSerializer(readiness).data,
                "team_rows": readiness.as_rows(),
            }
        )

    capacity_data = request.data.get("capacity", {})
    teams_data = request.data.get("teams", {})

    capacity_serializer = CapacityUpdateSerializer(capacity, data=capacity_data, partial=True)
    capacity_serializer.is_valid(raise_exception=True)
    readiness_serializer = ReadinessUpdateSerializer(readiness, data=teams_data, partial=True)
    readiness_serializer.is_valid(raise_exception=True)

    capacity_serializer.save(reported_at=timezone.now())
    readiness_serializer.save(reported_at=timezone.now())

    # The recommender reads capacity on every routing decision, and the
    # control room's board shows it, so an edit here has to reach both.
    board = dashboard_payload(hospital)
    broadcast(hospital_group(hospital.code), "capacity_updated", board)
    broadcast_ops("hospital_capacity", {"hospital": hospital.code, **board})

    return Response(
        {
            "hospital": {"id": hospital.id, "code": hospital.code, "name": hospital.name},
            "capacity": CapacityUpdateSerializer(capacity).data,
            "teams": ReadinessUpdateSerializer(readiness).data,
            "team_rows": readiness.as_rows(),
            "dashboard": board,
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def hospital_choices(request):
    """Hospitals this account may open.

    The pilot ships without ``staff_group`` bindings, so a single seeded
    hospital account has to be able to reach any ward's board to be useful for
    a demonstration. Where a binding does exist this returns exactly one row,
    and `resolve_hospital` ignores anything else the client asks for.
    """
    user = request.user
    bound = Hospital.objects.filter(staff_group__in=user.groups.all(), is_active=True)
    if bound.exists() and not has_role(user, Role.ADMIN):
        options = bound
    else:
        options = Hospital.objects.filter(is_active=True)

    return Response(
        {
            "hospitals": [
                {"id": h.id, "code": h.code, "name": h.name, "city": h.city}
                for h in options.order_by("name")
            ],
            "bound": bound.exists(),
        }
    )
