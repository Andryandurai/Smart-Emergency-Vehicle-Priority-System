"""Analytics REST surface (features 4.8 and 4.9)."""
from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from apps.analytics import exports, trends
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


# ---------------------------------------------------------------------------
# Phase 10 - chart-shaped analytics and exports
#
# All authenticated. The aggregate summary endpoints above are already
# role-gated and these carry the same class of information at finer grain -
# daily emergency volume and per-hospital load is operational intelligence,
# not public data, even though no individual trip is identifiable.
# ---------------------------------------------------------------------------
@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def daily_trends(request):
    """One point per day, plus the series catalogue the charts render from."""
    return Response(
        trends.daily_series(_days(request), request.query_params.get("city", "Chennai"))
    )


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def trend_summary(request):
    """Each metric's recent half against its previous half."""
    return Response(
        trends.trend(_days(request), request.query_params.get("city", "Chennai"))
    )


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def demand_profile(request):
    """When emergencies happen - hour of day and day of week, in local time."""
    return Response(trends.demand_profile(_days(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def distribution(request):
    """Emergency mix by category and priority level."""
    return Response(trends.category_distribution(_days(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def corridor_outcomes(request):
    """Preemptions per day, split by what actually happened."""
    return Response(trends.corridor_outcomes(_days(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def response_distribution(request):
    """Response-time histogram against the 8-minute target."""
    return Response(trends.response_distribution(_days(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def hospital_load(request):
    """Trips routed to each hospital, with the crew override rate."""
    return Response(trends.hospital_load(_days(request)))


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def export_catalogue(request):
    """What can be exported, and the exact columns each file will contain."""
    return Response({"datasets": exports.catalogue()})


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def export_csv(request, dataset: str):
    """Stream one dataset as CSV.

    404 rather than an empty file for an unknown dataset: a report pipeline
    that silently receives a zero-row CSV reports zero incidents, which is
    worse than reporting an error.
    """
    if dataset not in exports.DATASETS:
        return Response(
            {"detail": "Unknown dataset.", "available": sorted(exports.DATASETS)},
            status=404,
        )
    return exports.stream_csv(dataset, _days(request))
