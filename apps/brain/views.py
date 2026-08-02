"""REST surface of the AI Traffic Intelligence Engine."""
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.brain import graph as graph_mod
from apps.brain.congestion import build_forecaster, predictor_backend
from apps.brain.corridor import plan_corridor
from apps.brain.priority import rank_trips
from apps.brain.rerouting import evaluate_trip, reassess_active_trips
from apps.brain.router import RouteNotFound, route_between
from apps.brain.serializers import CongestionForecastQuerySerializer, RouteQuerySerializer
from apps.core.geo import Point
from apps.core.permissions import IsAuthenticatedRole, IsTrafficPolice, PublicRead


class RouteView(APIView):
    """Compute an optimised emergency route between two points."""

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RouteQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            route = route_between(
                Point(data["origin_lat"], data["origin_lon"]),
                Point(data["dest_lat"], data["dest_lon"]),
                priority_level=data["priority_level"],
                departure_at=data.get("departure_at"),
                algorithm=data.get("algorithm"),
                allow_contraflow=data["allow_contraflow"],
            )
        except RouteNotFound as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        return Response(route.as_dict(include_geometry=data["include_geometry"]))

    def get(self, request):
        """Query-string form, convenient for the map console and curl."""
        serializer = RouteQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        request._full_data = serializer.validated_data
        return self.post(request)


class CompareAlgorithmsView(APIView):
    """Run A* and Dijkstra on the same request.

    Both must return the same optimal duration (the A* heuristic is
    admissible); the interesting difference is nodes expanded.  Useful for
    validating the network import and for the technical write-up.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RouteQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        origin = Point(data["origin_lat"], data["origin_lon"])
        destination = Point(data["dest_lat"], data["dest_lon"])
        departure = data.get("departure_at") or timezone.now()

        results = {}
        for algorithm in ("astar", "dijkstra"):
            try:
                route = route_between(
                    origin,
                    destination,
                    priority_level=data["priority_level"],
                    departure_at=departure,
                    algorithm=algorithm,
                )
            except RouteNotFound as exc:
                results[algorithm] = {"error": str(exc)}
                continue
            results[algorithm] = {
                "total_duration_s": round(route.total_duration_s, 1),
                "total_distance_m": round(route.total_distance_m, 1),
                "nodes_expanded": route.nodes_expanded,
                "hop_count": len(route.steps),
            }
        return Response(results)


class CongestionForecastView(APIView):
    """Predict segment speeds at a future time (feature 4.4)."""

    permission_classes = [AllowAny]

    def post(self, request):
        from apps.network.models import RoadSegment

        serializer = CongestionForecastQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        horizon_s = data["minutes_ahead"] * 60

        state = graph_mod.get_state(force=True)
        edges = list(state.edges.values())

        if data.get("segment_ids"):
            wanted = set(data["segment_ids"])
            edges = [e for e in edges if e.segment_id in wanted]
        elif data.get("bbox"):
            min_lat, min_lon, max_lat, max_lon = data["bbox"]
            in_box = set(
                RoadSegment.objects.filter(
                    latitude__gte=min_lat, latitude__lte=max_lat,
                    longitude__gte=min_lon, longitude__lte=max_lon,
                ).values_list("id", flat=True)
            )
            edges = [e for e in edges if e.segment_id in in_box]

        edges = edges[: data["limit"]]
        forecaster = build_forecaster()
        forecasts = [forecaster.forecast(edge, horizon_s) for edge in edges]

        degrading = [
            f for f, e in zip(forecasts, edges)
            if e.live_kmh and f.speed_kmh < e.live_kmh * 0.75
        ]
        return Response(
            {
                "backend": predictor_backend(),
                "minutes_ahead": data["minutes_ahead"],
                "forecast_for": timezone.now() + timezone.timedelta(seconds=horizon_s),
                "segment_count": len(forecasts),
                "predicted_to_degrade": len(degrading),
                "forecasts": [f.as_dict() for f in forecasts],
            }
        )

    def get(self, request):
        serializer = CongestionForecastQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        request._full_data = serializer.validated_data
        return self.post(request)


@api_view(["GET"])
@permission_classes([PublicRead])
def network_summary(request):
    """Health and shape of the routable graph."""
    return Response(
        {
            **graph_mod.graph_summary(),
            "congestion_backend": predictor_backend(),
        }
    )


@api_view(["POST"])
@permission_classes([IsTrafficPolice])
def rebuild_graph(request):
    """Force a topology reload after importing or editing the network."""
    graph_mod.invalidate()
    return Response(graph_mod.graph_summary())


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def priority_ranking(request):
    """Current ranking across all active trips (feature 4.6)."""
    from apps.dispatch.models import EmergencyTrip

    trips = list(
        EmergencyTrip.objects.active().select_related("vehicle").prefetch_related("routes")
    )
    return Response({"ranking": [s.as_dict() for s in rank_trips(trips)]})


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def corridor_preview(request, trip_id: int):
    """What the green corridor for this trip looks like right now."""
    from apps.dispatch.models import EmergencyTrip

    trip = EmergencyTrip.objects.select_related("vehicle").get(pk=trip_id)
    return Response(plan_corridor(trip).as_dict())


@api_view(["POST"])
@permission_classes([IsTrafficPolice])
def evaluate_reroute(request, trip_id: int):
    """Ask whether a specific trip should be replanned right now."""
    from apps.dispatch.models import EmergencyTrip

    trip = EmergencyTrip.objects.select_related("vehicle").get(pk=trip_id)
    decision = evaluate_trip(trip, reason="manual request", force=bool(request.data.get("force")))
    payload = decision.as_dict()
    if decision.should_reroute and decision.route and request.data.get("apply"):
        from apps.dispatch.orchestrator import apply_new_route

        apply_new_route(trip, decision.route, reason=decision.reason)
        payload["applied"] = True
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsTrafficPolice])
def reassess_all(request):
    return Response({"results": reassess_active_trips(reason="operator request")})
