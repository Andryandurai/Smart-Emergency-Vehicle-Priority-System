"""Service-level endpoints: health probe and capability advertisement."""
from django.conf import settings
from django.db import connection
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.brain.congestion import predictor_backend
from apps.core.spatial import spatial_status
from apps.network.vision import cv_backend


class LivenessView(APIView):
    """Is this process alive? Nothing else.

    Deliberately checks **no** dependency. A liveness probe answers one
    question — should the orchestrator kill and restart this container — and
    the honest answer when the database is unreachable is *no*. Restarting the
    application cannot fix a database outage; it only removes a process that
    would have recovered on its own the moment the database came back, and in
    a rolling restart it can take down every replica at once over a fault none
    of them caused.

    This is the endpoint Docker's HEALTHCHECK and Kubernetes' livenessProbe
    should use. :class:`ReadinessView` is the one that checks dependencies.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"status": "alive"})


class ReadinessView(APIView):
    """Can this process serve traffic *right now*?

    Checks the dependencies a request actually needs. A failure here should
    take the instance out of the load balancer pool — not restart it.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        checks = {}
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover - requires a real outage
            checks["database"] = f"error: {exc}"

        try:
            from channels.layers import get_channel_layer

            layer = get_channel_layer()
            checks["channel_layer"] = layer.__class__.__name__ if layer else "missing"
        except Exception as exc:  # pragma: no cover
            checks["channel_layer"] = f"error: {exc}"

        ready = all(not str(value).startswith("error") for value in checks.values())
        return Response(
            {"status": "ready" if ready else "not-ready", "checks": checks},
            status=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class HealthView(APIView):
    """Combined health, kept for existing probes and the ops dashboard.

    Retained unchanged because load balancers, uptime monitors and the console
    already point at it. New deployments should use ``/health/live/`` for
    liveness and ``/health/ready/`` for readiness — see ``docs/DEPLOYMENT.md``.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        checks = {}
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover
            checks["database"] = f"error: {exc}"

        try:
            from channels.layers import get_channel_layer

            checks["channel_layer"] = (
                get_channel_layer().__class__.__name__ if get_channel_layer() else "missing"
            )
        except Exception as exc:  # pragma: no cover
            checks["channel_layer"] = f"error: {exc}"

        healthy = all(not str(v).startswith("error") for v in checks.values())
        return Response(
            {"status": "healthy" if healthy else "degraded", "checks": checks},
            status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


class ServiceInfoView(APIView):
    """Reports which optional subsystems are actually active in this install."""

    permission_classes = [AllowAny]

    def get(self, request):
        cfg = settings.SEVPS
        return Response(
            {
                "platform": "SEVPS - Smart Emergency Vehicle Priority System",
                "api_version": "v1",
                "layers": {
                    "1_vehicle_tracking": "active",
                    "2_ai_traffic_intelligence": "active",
                    "3_signal_control": "active",
                    "4_driver_alerts": "active",
                    "5_hospital_recommendation": "active",
                    "6_siren_priority_control": "active",
                },
                "backends": {
                    "database": settings.DATABASES["default"]["ENGINE"].rsplit(".", 1)[-1],
                    "spatial": spatial_status(),
                    "channel_layer": (
                        "redis" if settings.REDIS_URL else "in-memory"
                    ),
                    "routing_algorithm": cfg["ROUTE_ALGORITHM"],
                    "congestion_predictor": predictor_backend(),
                    "computer_vision": cv_backend(),
                    "traffic_provider": cfg["TRAFFIC_PROVIDER"],
                },
                "websockets": {
                    "ops": "/ws/ops/",
                    "hospital": "/ws/hospital/<code>/",
                    "vehicle": "/ws/vehicle/<callsign>/",
                    "drivers": "/ws/drivers/",
                    "signals": "/ws/signals/",
                },
            }
        )
