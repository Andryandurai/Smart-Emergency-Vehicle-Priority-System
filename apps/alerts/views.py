"""Layer 4 REST surface - device registration, alert lookup, board control."""
from django.db.models import F
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.alerts.dispatcher import active_alerts_near, clear_expired_boards
from apps.alerts.models import DisplayBoard, DriverAlert, DriverDevice
from apps.alerts.serializers import (
    DevicePositionSerializer,
    DisplayBoardSerializer,
    DriverAlertSerializer,
    DriverDeviceSerializer,
)
from apps.core.permissions import (
    IsAuthenticatedRole,
    IsTrafficPolice,
    PublicRead,
    PublicReadTrafficWrite,
)


class DisplayBoardViewSet(viewsets.ModelViewSet):
    """Roadside signs. Sign controllers poll without credentials."""

    queryset = DisplayBoard.objects.all()
    serializer_class = DisplayBoardSerializer
    permission_classes = [PublicReadTrafficWrite]

    @action(detail=False, methods=["post"], url_path="clear-expired", permission_classes=[IsTrafficPolice])
    def clear_expired(self, request):
        return Response({"cleared": clear_expired_boards()})

    @action(detail=False, methods=["get"], url_path="live", permission_classes=[PublicRead])
    def live(self, request):
        """What every board is showing right now - drives the board wall view."""
        boards = self.get_queryset().filter(is_active=True)
        return Response(
            [
                {
                    "code": b.code,
                    "name": b.name,
                    "latitude": b.latitude,
                    "longitude": b.longitude,
                    "message": b.current_message if b.is_displaying_alert else "",
                    "expires_at": b.message_expires_at,
                }
                for b in boards
            ]
        )


class DriverDeviceViewSet(viewsets.ModelViewSet):
    """Registered road-user devices. Device identifiers are personal data."""

    queryset = DriverDevice.objects.all()
    serializer_class = DriverDeviceSerializer
    lookup_field = "device_id"
    permission_classes = [IsAuthenticatedRole]


class DriverAlertViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DriverAlert.objects.select_related("trip", "board")
    serializer_class = DriverAlertSerializer
    #: Alert rows link to a trip id; the anonymous path is /alerts/nearby/.
    permission_classes = [IsAuthenticatedRole]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("live", "1") == "1" and self.action == "list":
            qs = qs.filter(expires_at__gt=timezone.now())
        trip = self.request.query_params.get("trip")
        if trip:
            qs = qs.filter(trip_id=trip)
        return qs


class DevicePositionView(APIView):
    """Road user reports position and receives any alerts that apply to it.

    This is the polling fallback for clients that cannot hold a WebSocket
    (navigation-app integrations, low-end devices, background modes).
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = DevicePositionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        device, _ = DriverDevice.objects.get_or_create(
            device_id=data["device_id"],
            defaults={"latitude": data["latitude"], "longitude": data["longitude"]},
        )
        if data.get("push_token"):
            device.push_token = data["push_token"]
        device.update_position(
            data["latitude"],
            data["longitude"],
            heading_deg=data.get("heading_deg", device.heading_deg),
            speed_kmh=data.get("speed_kmh", device.speed_kmh),
        )

        alerts = active_alerts_near(data["latitude"], data["longitude"])
        if alerts:
            DriverAlert.objects.filter(id__in=[a["id"] for a in alerts]).update(
                delivered_count=F("delivered_count") + 1
            )
        return Response(
            {"device_id": device.device_id, "alerts": alerts, "alert_count": len(alerts)},
            status=status.HTTP_200_OK,
        )


class NearbyAlertsView(APIView):
    """Read-only alert lookup - safe to expose to navigation integrations."""

    permission_classes = [AllowAny]

    def get(self, request):
        try:
            lat = float(request.query_params["lat"])
            lon = float(request.query_params["lon"])
        except (KeyError, ValueError):
            return Response(
                {"detail": "lat and lon query parameters are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        radius = float(request.query_params.get("radius_m", 1500))
        return Response({"alerts": active_alerts_near(lat, lon, radius)})
