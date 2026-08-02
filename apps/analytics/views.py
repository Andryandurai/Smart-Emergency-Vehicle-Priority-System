"""Analytics REST surface (features 4.8 and 4.9)."""
from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.analytics.models import DailyMetric, Hotspot
from apps.analytics.serializers import DailyMetricSerializer, HotspotSerializer
from apps.analytics.services import (
    congestion_hotspots,
    corridor_usage,
    dashboard_summary,
    emergency_movement_stats,
    high_delay_intersections,
    identify_accident_hotspots,
    response_time_stats,
    rollup_daily_metrics,
)
from apps.core.permissions import IsAuthenticatedRole, IsTrafficPolice


def _days(request, default: int = 30) -> int:
    try:
        return max(1, min(365, int(request.query_params.get("days", default))))
    except (TypeError, ValueError):
        return default


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def summary(request):
    return Response(dashboard_summary(_days(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def response_times(request):
    return Response(response_time_stats(_days(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def corridors(request):
    return Response(corridor_usage(_days(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def congestion(request):
    return Response({"hotspots": congestion_hotspots(_days(request, 14))})


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def delays(request):
    return Response({"intersections": high_delay_intersections(_days(request))})


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def movement(request):
    return Response(emergency_movement_stats(_days(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def accident_hotspots(request):
    """Stored accident hotspots - the map layer for feature 4.9."""
    stored = Hotspot.objects.filter(kind=Hotspot.Kind.ACCIDENT)
    return Response({"hotspots": HotspotSerializer(stored, many=True).data})


@api_view(["POST"])
@permission_classes([IsTrafficPolice])
def recompute_accident_hotspots(request):
    """Re-run the clustering over the accident archive."""
    clusters = identify_accident_hotspots(
        window_days=int(request.data.get("window_days", 180)),
        cell_m=float(request.data.get("cell_m", 250)),
        min_incidents=int(request.data.get("min_incidents", 3)),
    )
    return Response({"hotspots": clusters, "count": len(clusters)})


@api_view(["POST"])
@permission_classes([IsTrafficPolice])
def rollup(request):
    metric = rollup_daily_metrics()
    return Response(DailyMetricSerializer(metric).data)


class DailyMetricViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DailyMetric.objects.all()
    serializer_class = DailyMetricSerializer
    permission_classes = [IsAuthenticatedRole]


class HotspotViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Hotspot.objects.select_related("intersection")
    serializer_class = HotspotSerializer
    permission_classes = [IsAuthenticatedRole]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("kind"):
            qs = qs.filter(kind=self.request.query_params["kind"])
        return qs
