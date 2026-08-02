"""Layer 1 REST surface - fleet registry and live position ingestion."""
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import (
    IsAmbulanceCrew,
    IsAuthenticatedRole,
    IsCrewForVehicle,
    IsDispatcher,
)
from apps.core.realtime import broadcast_ops, broadcast, vehicle_group
from apps.fleet.models import EmergencyVehicle, Station, VehicleTelemetry
from apps.fleet.serializers import (
    EmergencyVehicleSerializer,
    StationSerializer,
    TelemetryIngestSerializer,
    VehicleStatusSerializer,
    VehicleTelemetrySerializer,
)


class StationViewSet(viewsets.ModelViewSet):
    """Ambulance bases. Read for any signed-in role; edited by dispatch."""

    queryset = Station.objects.all()
    serializer_class = StationSerializer
    permission_classes = [IsAuthenticatedRole]

    def get_permissions(self):
        if self.action in {'create', 'update', 'partial_update', 'destroy'}:
            return [IsDispatcher()]
        return [IsAuthenticatedRole()]


class EmergencyVehicleViewSet(viewsets.ModelViewSet):
    queryset = EmergencyVehicle.objects.select_related("home_station")
    serializer_class = EmergencyVehicleSerializer
    lookup_field = "pk"
    permission_classes = [IsAuthenticatedRole]

    #: Telemetry and status are crew actions; fleet registry edits are
    #: dispatch actions. Reads are open to any authenticated role.
    action_permissions = {
        'create': [IsDispatcher],
        'update': [IsDispatcher],
        'partial_update': [IsDispatcher],
        'destroy': [IsDispatcher],
        'ingest_telemetry': [IsCrewForVehicle],
        'set_status': [IsCrewForVehicle],
    }

    def get_permissions(self):
        classes = self.action_permissions.get(self.action, self.permission_classes)
        return [cls() for cls in classes]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("type"):
            qs = qs.filter(vehicle_type=params["type"])
        if params.get("available") == "1":
            qs = qs.deployable()
        if params.get("on_mission") == "1":
            qs = qs.on_mission()
        return qs

    @action(detail=True, methods=["post"], url_path="telemetry")
    def ingest_telemetry(self, request, pk=None):
        """Primary ingestion endpoint for the onboard unit / paramedic app.

        A single fix drives the whole reactive chain: position update ->
        route progress -> ETA refresh -> green corridor preemption ->
        driver alerts -> hospital dashboard update.
        """
        vehicle = self.get_object()
        serializer = TelemetryIngestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        telemetry = vehicle.record_position(
            data["latitude"],
            data["longitude"],
            speed_kmh=data.get("speed_kmh"),
            heading_deg=data.get("heading_deg"),
            accuracy_m=data.get("accuracy_m"),
            recorded_at=data.get("recorded_at"),
        )

        # Import locally: dispatch imports fleet, so a module-level import
        # here would create a cycle at app-load time.
        from apps.dispatch.orchestrator import on_vehicle_position

        outcome = on_vehicle_position(vehicle)

        payload = vehicle.as_tracking_payload()
        broadcast_ops("vehicle_position", payload)
        broadcast(vehicle_group(vehicle.callsign), "vehicle_position", payload)

        return Response(
            {
                "telemetry_id": telemetry.id,
                "vehicle": payload,
                "trip": outcome,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="status")
    def set_status(self, request, pk=None):
        vehicle = self.get_object()
        serializer = VehicleStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        vehicle.status = serializer.validated_data["status"]
        vehicle.save(update_fields=["status", "updated_at"])
        broadcast_ops("vehicle_status", vehicle.as_tracking_payload())
        return Response(EmergencyVehicleSerializer(vehicle).data)

    @action(detail=True, methods=["get"], url_path="track")
    def track(self, request, pk=None):
        """Recent breadcrumb trail for replay on the map."""
        vehicle = self.get_object()
        limit = min(int(request.query_params.get("limit", 200)), 2000)
        qs = VehicleTelemetry.objects.filter(vehicle=vehicle).order_by("-recorded_at")[:limit]
        points = list(reversed(VehicleTelemetrySerializer(qs, many=True).data))
        return Response({"vehicle": vehicle.callsign, "points": points})

    @action(detail=True, methods=["get"], url_path="projection")
    def projection(self, request, pk=None):
        """Predicted future position - the core of 'predicts its future location'."""
        vehicle = self.get_object()
        horizon = float(request.query_params.get("seconds", 60))

        from apps.brain.eta import project_vehicle

        projected, method = project_vehicle(vehicle, horizon)
        return Response(
            {
                "vehicle": vehicle.callsign,
                "seconds_ahead": horizon,
                "method": method,
                "latitude": projected.lat,
                "longitude": projected.lon,
            }
        )

    @action(detail=False, methods=["get"], url_path="live")
    def live(self, request):
        """Snapshot of every online vehicle - bootstraps the ops map."""
        vehicles = self.get_queryset().online()
        return Response(
            {
                "generated_at": timezone.now(),
                "count": vehicles.count(),
                "vehicles": [v.as_tracking_payload() for v in vehicles],
            }
        )

    @action(detail=False, methods=["get"], url_path="nearest")
    def nearest(self, request):
        """Closest deployable vehicles to an incident location."""
        try:
            lat = float(request.query_params["lat"])
            lon = float(request.query_params["lon"])
        except (KeyError, ValueError):
            return Response(
                {"detail": "lat and lon query parameters are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        radius = float(request.query_params.get("radius_m", 15000))
        vehicle_type = request.query_params.get("type")

        qs = EmergencyVehicle.objects.deployable()
        if vehicle_type:
            qs = qs.filter(vehicle_type=vehicle_type)

        found = qs.near(lat, lon, radius)[: int(request.query_params.get("limit", 5))]
        return Response(
            [
                {**v.as_tracking_payload(), "distance_m": round(v.distance_m, 1)}
                for v in found
            ]
        )
