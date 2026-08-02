"""Prediction API.

Every response carries the same three keys - ``prediction``, ``confidence``,
``explanation`` - because a client must never be able to consume a value
without the caveats attached to it.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.brain.ml import estimators as est
from apps.brain.ml import services
from apps.core.permissions import IsAuthenticatedRole, PublicRead


class ModelStatusView(APIView):
    """``GET /api/v1/brain/ml/models/`` - which models are trained and live."""

    permission_classes = [PublicRead]

    def get(self, request):
        return Response(
            {
                "models": est.registry_status(),
                "note": (
                    "A model reported as unavailable falls back to a statistical "
                    "baseline. Predictions state which produced them via `source`."
                ),
            }
        )


class CongestionPredictionView(APIView):
    """``GET /api/v1/brain/ml/congestion/?segment=1&minutes=15``"""

    permission_classes = [PublicRead]

    def get(self, request):
        try:
            segment_id = int(request.query_params["segment"])
        except (KeyError, ValueError):
            return Response(
                {"detail": "segment query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        minutes = float(request.query_params.get("minutes", 10))
        try:
            prediction = services.predict_congestion(segment_id, minutes)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(prediction.as_dict())


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def predict_trip_eta(request, trip_id: int):
    """``GET /api/v1/brain/ml/eta/<trip_id>/`` - router ETA plus learned correction."""
    from apps.dispatch.models import EmergencyTrip

    trip = (
        EmergencyTrip.objects.select_related("vehicle")
        .prefetch_related("routes")
        .filter(pk=trip_id)
        .first()
    )
    if trip is None:
        return Response({"detail": "trip not found"}, status=status.HTTP_404_NOT_FOUND)
    try:
        return Response(services.predict_eta(trip).as_dict())
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)


class PriorityPredictionView(APIView):
    """``POST /api/v1/brain/ml/priority/`` - advisory Layer 6 level.

    The response always reports the *rule* level as the prediction. Any model
    disagreement is surfaced in ``disagreement`` for a human to weigh, never
    acted on automatically.
    """

    permission_classes = [IsAuthenticatedRole]

    def post(self, request):
        category = request.data.get("emergency_category")
        if not category:
            return Response(
                {"detail": "emergency_category is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        prediction = services.predict_priority(
            category,
            patient_age=request.data.get("patient_age"),
            deteriorating=bool(request.data.get("patient_deteriorating")),
        )
        return Response(prediction.as_dict())


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def predict_corridor(request, signal_id: int):
    """``GET /api/v1/brain/ml/corridor/<signal_id>/?seconds=45&priority=1``"""
    from apps.network.models import TrafficSignal

    signal = TrafficSignal.objects.filter(pk=signal_id).first()
    if signal is None:
        return Response({"detail": "signal not found"}, status=status.HTTP_404_NOT_FOUND)

    prediction = services.predict_corridor_success(
        signal,
        seconds_to_arrival=float(request.query_params.get("seconds", 45)),
        priority_level=int(request.query_params.get("priority", 1)),
        congestion_index=float(request.query_params.get("congestion", 0.0)),
        lanes=int(request.query_params.get("lanes", 2)),
    )
    return Response(prediction.as_dict())


class ExplainedRecommendationView(APIView):
    """``POST /api/v1/brain/ml/hospital/`` - recommendation in the same envelope.

    Deliberately no SHAP: the recommender is a deterministic weighted sum whose
    terms are already published per candidate, so the explanation reports the
    real weighted contributions rather than an approximation of them.
    """

    permission_classes = [PublicRead]

    def get_permissions(self):
        from rest_framework.permissions import AllowAny

        # A POST body carrying a location and a category, not a mutation.
        return [AllowAny()]

    def post(self, request):
        from apps.core.geo import Point
        from apps.hospitals.recommender import recommend_hospital

        try:
            latitude = float(request.data["latitude"])
            longitude = float(request.data["longitude"])
            category = request.data["emergency_category"]
        except (KeyError, TypeError, ValueError):
            return Response(
                {"detail": "latitude, longitude and emergency_category are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        recommendation = recommend_hospital(Point(latitude, longitude), category)
        prediction = services.explain_hospital_recommendation(recommendation)
        return Response(
            {**prediction.as_dict(), "candidates": [c.as_dict() for c in recommendation.candidates]}
        )
