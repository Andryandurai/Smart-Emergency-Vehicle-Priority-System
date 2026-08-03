"""Layer 5 REST surface - rule base, hospital registry, recommendation, alerts."""
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.geo import Point
from apps.core.permissions import (
    IsAdministrator,
    IsAuthenticatedRole,
    IsHospitalStaff,
    PublicRead,
    PublicReadHospitalWrite,
)
from apps.core.realtime import broadcast, broadcast_ops, hospital_group
from apps.hospitals.models import (
    EmergencyRule,
    Hospital,
    HospitalAlert,
    HospitalCapability,
    HospitalCapacity,
    HospitalRecommendationLog,
)
from apps.hospitals.recommender import recommend_hospital
from apps.hospitals.rules import resolve_rule, seed_rules
from apps.hospitals.symptoms import assess
from apps.hospitals.symptoms import catalogue as symptom_catalogue
from apps.hospitals.serializers import (
    EmergencyRuleSerializer,
    HospitalAlertSerializer,
    HospitalCapabilitySerializer,
    HospitalCapacitySerializer,
    HospitalRecommendationLogSerializer,
    HospitalSerializer,
    RecommendationRequestSerializer,
)


class HospitalViewSet(viewsets.ModelViewSet):
    queryset = (
        Hospital.objects.prefetch_related("capabilities").select_related("capacity_row").all()
    )
    serializer_class = HospitalSerializer
    #: The capability directory is public (it is what /recommend/ returns);
    #: capacity and diversion are written by the hospital itself.
    permission_classes = [PublicReadHospitalWrite]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("city"):
            qs = qs.filter(city__iexact=params["city"])
        if params.get("active", "1") == "1":
            qs = qs.filter(is_active=True)
        if params.get("facility"):
            qs = qs.filter(
                capabilities__facility=params["facility"], capabilities__is_available=True
            )
        return qs.distinct()

    @action(detail=True, methods=["get", "post", "patch"], url_path="capacity")
    def capacity(self, request, pk=None):
        """Read or update live bed availability / workload.

        This is the endpoint a hospital's own HIS integrates with; an accurate
        feed here is what makes the recommendation trustworthy.
        """
        hospital = self.get_object()
        capacity, _ = HospitalCapacity.objects.get_or_create(hospital=hospital)
        if request.method == "GET":
            return Response(HospitalCapacitySerializer(capacity).data)

        self.check_permissions(request)
        serializer = HospitalCapacitySerializer(capacity, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(reported_at=timezone.now())
        payload = serializer.data
        broadcast(hospital_group(hospital.code), "capacity_updated", payload)
        broadcast_ops("hospital_capacity", {"hospital": hospital.code, **payload})
        return Response(payload)

    @action(detail=True, methods=["post"], url_path="diversion", permission_classes=[IsHospitalStaff])
    def set_diversion(self, request, pk=None):
        """Declare (or lift) diversion status - excludes the hospital from ranking."""
        hospital = self.get_object()
        hospital.is_on_diversion = bool(request.data.get("is_on_diversion", True))
        hospital.diversion_reason = request.data.get("reason", "")
        hospital.save(update_fields=["is_on_diversion", "diversion_reason", "updated_at"])
        broadcast_ops(
            "hospital_diversion",
            {
                "hospital": hospital.code,
                "is_on_diversion": hospital.is_on_diversion,
                "reason": hospital.diversion_reason,
            },
        )
        return Response(HospitalSerializer(hospital).data)

    @action(
        detail=True, methods=["get"], url_path="inbound",
        permission_classes=[IsAuthenticatedRole],
    )
    def inbound(self, request, pk=None):
        """Trips currently heading to this hospital - the preparedness feed.

        Authenticated: these are identifiable patients. The serializer
        additionally redacts clinical fields for roles without clearance,
        which is why the request must be passed into its context.
        """
        from apps.dispatch.models import EmergencyTrip
        from apps.dispatch.serializers import EmergencyTripSerializer

        hospital = self.get_object()
        trips = (
            EmergencyTrip.objects.active()
            .filter(destination_hospital=hospital)
            .select_related("vehicle", "destination_hospital")
        )
        return Response(
            EmergencyTripSerializer(
                trips, many=True, context={"request": request}
            ).data
        )


class HospitalCapabilityViewSet(viewsets.ModelViewSet):
    queryset = HospitalCapability.objects.select_related("hospital")
    serializer_class = HospitalCapabilitySerializer
    permission_classes = [PublicReadHospitalWrite]


class EmergencyRuleViewSet(viewsets.ModelViewSet):
    """The Rule-Based Emergency Engine's rule base, editable by governance."""

    queryset = EmergencyRule.objects.all()
    serializer_class = EmergencyRuleSerializer
    lookup_field = "category"
    #: The rule base is published clinical reference data; only clinical
    #: governance (administrators) may change it.
    permission_classes = [IsAdministrator]

    def get_permissions(self):
        if self.request.method in ('GET', 'HEAD', 'OPTIONS'):
            return [PublicRead()]
        return [IsAdministrator()]

    @action(detail=False, methods=["post"], url_path="seed")
    def seed(self, request):
        created, updated = seed_rules()
        return Response({"created": created, "updated": updated})

    @action(detail=False, methods=["get"], url_path="catalogue", permission_classes=[PublicRead])
    def catalogue(self, request):
        """Category picker contents for the paramedic app."""
        rules = EmergencyRule.objects.filter(is_active=True)
        if not rules.exists():
            from apps.hospitals.rules import DEFAULT_RULES

            return Response(
                [
                    {
                        "category": r["category"],
                        "display_name": r["display_name"],
                        "required_facilities": list(r["required_facilities"]),
                        "default_priority_level": int(r["default_priority_level"]),
                        "guidance": r["guidance"],
                    }
                    for r in DEFAULT_RULES
                ]
            )
        return Response(EmergencyRuleSerializer(rules, many=True).data)


class RecommendHospitalView(APIView):
    """The core Layer 5 call: category + location -> ranked hospitals.

    Public by design: the request carries a location and a category, never
    a patient record, and the response is public hospital capability. A
    crew must be able to get this answer even on an unauthenticated
    fallback device.
    """

    permission_classes = [PublicRead]

    def get_permissions(self):
        # PublicRead only allows safe methods; this endpoint is a POST
        # because the query is structured, not because it mutates state.
        return [AllowAny()]

    def post(self, request):
        serializer = RecommendationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        recommendation = recommend_hospital(
            Point(data["latitude"], data["longitude"]),
            data["emergency_category"],
            radius_km=data.get("radius_km"),
            max_candidates=data.get("max_candidates"),
            exclude_hospital_ids=set(data.get("exclude_hospital_ids") or []),
            symptoms=data.get("symptoms"),
        )
        payload = recommendation.as_dict()
        # Echo what the symptoms contributed, so the crew can see why the
        # shortlist narrowed rather than having to trust that it did.
        payload["symptom_assessment"] = assess(data.get("symptoms")).as_dict()
        return Response(payload)


class RuleLookupView(APIView):
    """Resolve one category to its clinical requirements (no hospital search)."""

    permission_classes = [PublicRead]

    def get(self, request, category: str):
        return Response(resolve_rule(category).as_dict())


class SymptomCatalogueView(APIView):
    """The symptom picker's contents.

    Public for the same reason the rule catalogue is: it is clinical
    configuration, not patient data, and a crew on an unauthenticated
    fallback device still has to be able to record what they can see.
    """

    permission_classes = [PublicRead]

    def get(self, request):
        return Response({"symptoms": symptom_catalogue()})


class HospitalAlertViewSet(viewsets.ModelViewSet):
    queryset = HospitalAlert.objects.select_related("hospital", "trip", "trip__vehicle")
    serializer_class = HospitalAlertSerializer
    #: Alerts name the emergency category of an identified inbound patient.
    permission_classes = [IsAuthenticatedRole]

    def get_queryset(self):
        qs = super().get_queryset()
        code = self.request.query_params.get("hospital")
        if code:
            qs = qs.filter(hospital__code__iexact=code)
        if self.request.query_params.get("pending") == "1":
            qs = qs.filter(acknowledged_at__isnull=True)
        return qs

    @action(detail=True, methods=["post"], permission_classes=[IsHospitalStaff])
    def acknowledge(self, request, pk=None):
        """Hospital confirms it has seen the inbound patient and is preparing."""
        alert = self.get_object()
        alert.acknowledged_at = timezone.now()
        alert.acknowledged_by = request.data.get(
            "acknowledged_by", request.user.get_username() if request.user.is_authenticated else ""
        )
        alert.preparation_notes = request.data.get("preparation_notes", alert.preparation_notes)
        alert.save(
            update_fields=["acknowledged_at", "acknowledged_by", "preparation_notes", "updated_at"]
        )
        payload = HospitalAlertSerializer(alert).data
        broadcast_ops("hospital_ack", payload)
        broadcast(hospital_group(alert.hospital.code), "alert_acknowledged", payload)
        return Response(payload, status=status.HTTP_200_OK)


class HospitalRecommendationLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = HospitalRecommendationLog.objects.select_related("recommended", "chosen", "trip")
    serializer_class = HospitalRecommendationLogSerializer
    #: Clinical governance audit trail - tied to identifiable trips.
    permission_classes = [IsAuthenticatedRole]
