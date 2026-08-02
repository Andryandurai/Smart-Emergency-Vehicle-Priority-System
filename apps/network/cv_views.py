"""Computer-vision REST surface (Phase 7)."""
from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import IsAuthenticatedRole, IsTrafficPolice, PublicRead
from apps.core.realtime import broadcast_ops
from apps.network.cv import backends, pipeline
from apps.network.models import CameraFeed


class VisionStatusView(APIView):
    """``GET /api/v1/network/cv/status/`` - backend and camera estate health."""

    permission_classes = [PublicRead]

    def get(self, request):
        cameras = CameraFeed.objects.filter(is_active=True)
        return Response(
            {
                "backend": backends.backend_name(),
                "yolo_active": backends.is_yolo_active(),
                "cameras": {
                    "total": CameraFeed.objects.count(),
                    "active": cameras.count(),
                    "with_segment": cameras.filter(segment__isnull=False).count(),
                    "never_analysed": cameras.filter(last_analysed_at__isnull=True).count(),
                },
                "detections": [
                    "vehicle", "emergency_vehicle", "traffic_density",
                    "road_block", "illegal_parking", "accident",
                ],
                "note": (
                    "In simulated mode detections are synthesised from each "
                    "segment's live state and an urban demand curve. Set "
                    "SEVPS_CV_MODE=yolo with ultralytics installed for real inference."
                ),
            }
        )


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def analyse_one(request, camera_id: int):
    """``GET /api/v1/network/cv/cameras/<id>/analyse/`` - analyse without persisting.

    Read-only on purpose: an operator inspecting what a camera currently sees
    should not create road events as a side effect of looking.
    """
    camera = CameraFeed.objects.select_related("segment").filter(pk=camera_id).first()
    if camera is None:
        return Response({"detail": "camera not found"}, status=status.HTTP_404_NOT_FOUND)

    analysis = pipeline.analyse_camera(camera)
    return Response(
        {
            "camera": {"id": camera.id, "name": camera.name},
            "analysis": analysis.as_dict(),
            "detections": [d.as_dict() for d in analysis.detections[:50]],
            "would_create_events": [
                f.kind for f in analysis.actionable_findings
                if f.kind != "emergency_vehicle"
            ],
        }
    )


@api_view(["POST"])
@permission_classes([IsTrafficPolice])
def sweep_cameras(request):
    """``POST /api/v1/network/cv/sweep/`` - analyse and ingest across the estate."""
    camera_ids = request.data.get("camera_ids")
    cameras = CameraFeed.objects.filter(is_active=True).select_related("segment")
    if camera_ids:
        cameras = cameras.filter(id__in=camera_ids)

    result = pipeline.sweep(cameras)
    if result["events_created"] or result["emergency_sightings"]:
        broadcast_ops("cv_sweep", result)
    return Response(result)


@api_view(["GET"])
@permission_classes([IsAuthenticatedRole])
def emergency_sightings(request):
    """``GET /api/v1/network/cv/emergency/`` - where cameras can see our vehicles.

    Corroborates the fleet's own telemetry: a vehicle whose GPS says it is at a
    junction and which a camera there can also see is confirmed to be on that
    carriageway rather than a parallel road.
    """
    cameras = CameraFeed.objects.filter(is_active=True).select_related("segment")
    sightings = []
    for camera in cameras:
        analysis = pipeline.analyse_camera(camera)
        for sighting in analysis.emergency_vehicles:
            sightings.append(
                {
                    **sighting,
                    "camera": camera.name,
                    "latitude": camera.latitude,
                    "longitude": camera.longitude,
                }
            )
    return Response({"sightings": sightings, "count": len(sightings)})
