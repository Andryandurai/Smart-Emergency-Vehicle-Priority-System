"""Dispatch REST surface - trips, routes, corridors and priority directives."""
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.core.enums import PatientSymptom, TripStage, VehicleStatus
from apps.core.geo import Point
from apps.core.permissions import (
    IsAmbulanceCrew,
    IsAuthenticatedRole,
    IsDispatcher,
    IsOperator,
    IsTrafficPolice,
    PublicRead,
)
from apps.dispatch import orchestrator
from apps.dispatch.corridor import corridor_status, release_corridor, sync_corridor, tick_corridors
from apps.dispatch.models import EmergencyTrip, PriorityDirective, RoutePlan, SignalPreemption
from apps.dispatch.serializers import (
    AssessPatientSerializer,
    ConditionUpdateSerializer,
    CreateTripSerializer,
    EmergencyTripSerializer,
    PriorityDirectiveSerializer,
    RoutePlanSerializer,
    SignalPreemptionSerializer,
    StageChangeSerializer,
)
from apps.dispatch.siren import PRIORITY_PROFILES, apply_priority
from apps.fleet.models import EmergencyVehicle


class EmergencyTripViewSet(viewsets.ModelViewSet):
    """Emergency responses.

    Reads require authentication - a trip carries patient-identifying data,
    and clinical fields are additionally redacted for roles without clearance
    (see ``ClinicalRedactionMixin``). Writes are split by duty: dispatch opens
    and closes responses, crew perform clinical actions, traffic control
    commands the corridor.
    """

    queryset = EmergencyTrip.objects.select_related(
        "vehicle", "destination_hospital"
    ).prefetch_related("routes")
    serializer_class = EmergencyTripSerializer
    permission_classes = [IsAuthenticatedRole]

    #: action -> policy. Anything unlisted falls back to permission_classes.
    action_permissions = {
        "create": [IsDispatcher],
        "destroy": [IsDispatcher],
        "cancel": [IsDispatcher],
        "update": [IsDispatcher],
        "partial_update": [IsDispatcher],
        "assess": [IsAmbulanceCrew],
        "change_stage": [IsAmbulanceCrew],
        "update_condition": [IsAmbulanceCrew],
        "handover": [IsAmbulanceCrew],
        "reroute": [IsAmbulanceCrew],
        "corridor_sync": [IsTrafficPolice],
        "corridor_release": [IsTrafficPolice],
    }

    def get_permissions(self):
        classes = self.action_permissions.get(self.action, self.permission_classes)
        return [cls() for cls in classes]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("active") == "1":
            qs = qs.active()
        if params.get("stage"):
            qs = qs.filter(stage=params["stage"])
        if params.get("hospital"):
            qs = qs.filter(destination_hospital__code__iexact=params["hospital"])
        if params.get("vehicle"):
            qs = qs.filter(vehicle__callsign__iexact=params["vehicle"])
        return qs

    def create(self, request, *args, **kwargs):
        """Open a trip (control-room call-out)."""
        serializer = CreateTripSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        incident = Point(data["incident_latitude"], data["incident_longitude"])

        vehicle = self._resolve_vehicle(data, incident)
        if vehicle is None:
            return Response(
                {"detail": "No available vehicle could be assigned."},
                status=status.HTTP_409_CONFLICT,
            )

        trip = orchestrator.create_trip(
            vehicle=vehicle,
            incident_point=incident,
            emergency_category=data["emergency_category"],
            incident_address=data.get("incident_address", ""),
            caller_number=data.get("caller_number", ""),
            patient_age=data.get("patient_age"),
            patient_notes=data.get("patient_notes", ""),
        )
        return Response(
            EmergencyTripSerializer(trip, context={"request": request}).data, status=status.HTTP_201_CREATED
        )

    @staticmethod
    def _resolve_vehicle(data, incident: Point):
        if data.get("vehicle_id"):
            return EmergencyVehicle.objects.filter(pk=data["vehicle_id"]).first()
        if data.get("vehicle_callsign"):
            return EmergencyVehicle.objects.filter(
                callsign__iexact=data["vehicle_callsign"]
            ).first()
        # auto_assign: nearest *dispatchable* unit within 25 km. Dispatchable,
        # not merely deployable: a vehicle grounded by a failed brake check is
        # idle at the station and would otherwise be the nearest thing to the
        # call, which is precisely the outcome the readiness gate exists to
        # prevent.
        nearest = EmergencyVehicle.objects.dispatchable().near(
            incident.lat, incident.lon, 25_000
        )
        return nearest[0] if nearest else None

    @action(detail=True, methods=["post"])
    def assess(self, request, pk=None):
        """Paramedic's preliminary assessment - the Layer 5 entry point.

        Selecting the emergency category is the single action that triggers
        hospital selection, route optimisation, the green corridor and the
        light/siren mode.
        """
        trip = self.get_object()
        serializer = AssessPatientSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        for field in ("patient_age", "patient_notes", "patient_deteriorating", "allow_contraflow"):
            if field in data:
                setattr(trip, field, data[field])
        trip.emergency_category = data["emergency_category"]
        if "symptoms" in data:
            # Deduplicated and ordered by the enum, so the same observations
            # always store and display identically.
            selected = set(data["symptoms"])
            trip.symptoms = [s for s, _ in PatientSymptom.choices if s in selected]
        trip.save(
            update_fields=[
                "emergency_category", "symptoms", "patient_age", "patient_notes",
                "patient_deteriorating", "allow_contraflow", "updated_at",
            ]
        )

        hospital = None
        if data.get("hospital_id"):
            from apps.hospitals.models import Hospital

            hospital = get_object_or_404(Hospital, pk=data["hospital_id"])

        result = orchestrator.assign_hospital(
            trip,
            hospital=hospital,
            emergency_category=data["emergency_category"],
            override_reason=data.get("override_reason", ""),
            choice_reason=data.get("choice_reason"),
        )
        trip.refresh_from_db()
        return Response(
            {
                "trip": EmergencyTripSerializer(trip, context={"request": request}).data,
                "recommendation": result.get("recommendation"),
                "corridor": corridor_status(trip),
            }
        )

    @action(detail=True, methods=["post"], url_path="stage")
    def change_stage(self, request, pk=None):
        trip = self.get_object()
        serializer = StageChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        orchestrator.advance_stage(
            trip, serializer.validated_data["stage"],
            reason=serializer.validated_data.get("reason", ""),
        )
        trip.refresh_from_db()
        return Response(EmergencyTripSerializer(trip, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="condition")
    def update_condition(self, request, pk=None):
        """Re-triage in transit - Layer 6 upgrades or downgrades accordingly."""
        trip = self.get_object()
        serializer = ConditionUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        fields = ["updated_at"]
        if "patient_deteriorating" in data:
            trip.patient_deteriorating = data["patient_deteriorating"]
            fields.append("patient_deteriorating")
        if data.get("emergency_category"):
            trip.emergency_category = data["emergency_category"]
            fields.append("emergency_category")
        if data.get("notes"):
            trip.patient_notes = (trip.patient_notes + "\n" + data["notes"]).strip()
            fields.append("patient_notes")
        trip.save(update_fields=fields)

        directive = apply_priority(
            trip,
            level=data.get("priority_level"),
            trigger="crew condition update",
            issued_by=request.user.get_username() if request.user.is_authenticated else "crew",
            force=True,
        )
        # Priority changed -> corridor entitlement changed.
        corridor = sync_corridor(trip)
        return Response(
            {
                "trip": EmergencyTripSerializer(trip, context={"request": request}).data,
                "directive": PriorityDirectiveSerializer(directive).data if directive else None,
                "corridor": corridor,
            }
        )

    @action(detail=True, methods=["post"], url_path="reroute")
    def reroute(self, request, pk=None):
        """Force a fresh route computation from the vehicle's current position."""
        trip = self.get_object()
        destination = trip.destination_point
        if destination is None:
            return Response(
                {"detail": "Trip has no destination yet."}, status=status.HTTP_400_BAD_REQUEST
            )
        plan = orchestrator.plan_route_to(
            trip, destination, reason=request.data.get("reason", "manual reroute")
        )
        if plan is None:
            return Response(
                {"detail": "No route could be computed."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        return Response(RoutePlanSerializer(plan).data)

    @action(detail=True, methods=["get"], url_path="corridor")
    def corridor(self, request, pk=None):
        return Response({"corridor": corridor_status(self.get_object())})

    # Per-action policy lives in `action_permissions` above, so there is a
    # single table to audit rather than decorators scattered through the file.
    @action(detail=True, methods=["post"], url_path="corridor/sync")
    def corridor_sync(self, request, pk=None):
        return Response(sync_corridor(self.get_object()))

    @action(detail=True, methods=["post"], url_path="corridor/release")
    def corridor_release(self, request, pk=None):
        return Response(
            release_corridor(
                self.get_object(), reason=request.data.get("reason", "operator release")
            )
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        trip = self.get_object()
        orchestrator.advance_stage(
            trip, TripStage.CANCELLED, reason=request.data.get("reason", "cancelled by operator")
        )
        return Response(EmergencyTripSerializer(trip, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="handover")
    def handover(self, request, pk=None):
        """Patient handed over - closes the trip and frees the vehicle."""
        trip = self.get_object()
        orchestrator.advance_stage(trip, TripStage.HANDOVER, reason="patient handover complete")
        trip.vehicle.status = VehicleStatus.AVAILABLE
        trip.vehicle.save(update_fields=["status", "updated_at"])
        return Response(EmergencyTripSerializer(trip, context={"request": request}).data)

    @action(detail=False, methods=["get"], url_path="live")
    def live(self, request):
        """Everything the Emergency Operations Dashboard needs in one call.

        Authentication required: a trip carries patient-identifying data, so
        this cannot be an anonymous feed. Clinical fields are additionally
        redacted for roles without clearance.
        """
        trips = self.get_queryset().active()
        return Response(
            {
                "count": trips.count(),
                "trips": EmergencyTripSerializer(
                    trips, many=True, context={"request": request}
                ).data,
            }
        )


class RoutePlanViewSet(viewsets.ReadOnlyModelViewSet):
    """Computed routes. Operational, not clinical - any signed-in role may read."""

    queryset = RoutePlan.objects.select_related("trip")
    serializer_class = RoutePlanSerializer
    permission_classes = [IsAuthenticatedRole]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("trip"):
            qs = qs.filter(trip_id=self.request.query_params["trip"])
        if self.request.query_params.get("active") == "1":
            qs = qs.filter(is_active=True)
        return qs


class SignalPreemptionViewSet(viewsets.ReadOnlyModelViewSet):
    """Green corridor audit trail - every second cross traffic was held."""

    queryset = SignalPreemption.objects.select_related(
        "signal", "signal__intersection", "trip", "trip__vehicle"
    )
    serializer_class = SignalPreemptionSerializer
    permission_classes = [IsAuthenticatedRole]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("state"):
            qs = qs.filter(state=params["state"])
        if params.get("trip"):
            qs = qs.filter(trip_id=params["trip"])
        if params.get("open") == "1":
            qs = qs.filter(state__in=["planned", "armed", "active"])
        return qs


class PriorityDirectiveViewSet(viewsets.ReadOnlyModelViewSet):
    """Layer 6 decision log. Directives reference clinical triggers."""

    queryset = PriorityDirective.objects.select_related("trip", "trip__vehicle")
    serializer_class = PriorityDirectiveSerializer
    permission_classes = [IsAuthenticatedRole]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("trip"):
            qs = qs.filter(trip_id=self.request.query_params["trip"])
        return qs


@api_view(["GET"])
@permission_classes([PublicRead])
def priority_profiles(request):
    """The Layer 6 ladder - static documentation, safe to publish."""
    return Response([p.as_dict() for p in PRIORITY_PROFILES.values()])


@api_view(["POST"])
@permission_classes([IsTrafficPolice])
def corridor_tick(request):
    """Safety sweep endpoint - release expired holds (also run by the ticker)."""
    return Response(tick_corridors())
