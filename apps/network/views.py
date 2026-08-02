"""REST surface for the road graph, signals, sensing and disruptions."""
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import (
    IsAuthenticatedRole,
    IsTrafficPolice,
    PublicRead,
    PublicReadTrafficWrite,
)
from apps.core.realtime import broadcast_ops
from apps.network.models import (
    AccidentRecord,
    CameraFeed,
    Intersection,
    RoadEvent,
    RoadSegment,
    TrafficObservation,
    TrafficSignal,
)
from apps.network.serializers import (
    AccidentRecordSerializer,
    CameraAnalysisRequestSerializer,
    CameraFeedSerializer,
    IntersectionSerializer,
    RoadEventSerializer,
    RoadSegmentGeoSerializer,
    RoadSegmentSerializer,
    SpeedUpdateSerializer,
    TrafficObservationSerializer,
    TrafficSignalSerializer,
)
from apps.network.vision import analyse_camera, ingest_camera_analysis


class IntersectionViewSet(viewsets.ModelViewSet):
    queryset = Intersection.objects.all()
    serializer_class = IntersectionSerializer
    permission_classes = [PublicReadTrafficWrite]
    filterset_fields = ["city", "is_signalised"]

    def get_queryset(self):
        qs = super().get_queryset()
        city = self.request.query_params.get("city")
        if city:
            qs = qs.filter(city__iexact=city)
        if self.request.query_params.get("signalised") == "1":
            qs = qs.filter(is_signalised=True)
        return qs


class RoadSegmentViewSet(viewsets.ModelViewSet):
    queryset = RoadSegment.objects.select_related("from_node", "to_node")
    serializer_class = RoadSegmentSerializer
    permission_classes = [PublicReadTrafficWrite]

    def get_queryset(self):
        qs = super().get_queryset()
        level = self.request.query_params.get("congestion")
        if level:
            qs = qs.filter(congestion_level=level)
        if self.request.query_params.get("open") == "0":
            qs = qs.filter(is_open=False)
        return qs

    @action(detail=False, methods=["get"], url_path="geojson", permission_classes=[PublicRead])
    def geojson(self, request):
        """The whole network as a FeatureCollection for the dashboard map."""
        qs = self.get_queryset()
        limit = int(request.query_params.get("limit", 2000))
        features = RoadSegmentGeoSerializer(qs[:limit], many=True).data
        return Response({"type": "FeatureCollection", "features": features})

    @action(detail=False, methods=["post"], url_path="speeds", permission_classes=[IsTrafficPolice])
    def ingest_speeds(self, request):
        """Bulk live-speed ingestion (probe data / external provider feed)."""
        serializer = SpeedUpdateSerializer(data=request.data, many=True)
        serializer.is_valid(raise_exception=True)
        by_id = {row["segment_id"]: row["speed_kmh"] for row in serializer.validated_data}
        segments = RoadSegment.objects.filter(id__in=by_id)
        updated = 0
        for segment in segments:
            segment.apply_speed(by_id[segment.id])
            updated += 1
        return Response({"updated": updated})


class TrafficSignalViewSet(viewsets.ModelViewSet):
    queryset = TrafficSignal.objects.select_related("intersection")
    serializer_class = TrafficSignalSerializer
    permission_classes = [PublicReadTrafficWrite]

    @action(detail=True, methods=["post"], permission_classes=[IsTrafficPolice])
    def heartbeat(self, request, pk=None):
        """Controller check-in - keeps the signal eligible for preemption."""
        signal = self.get_object()
        signal.last_heartbeat = timezone.now()
        signal.is_online = True
        phase = request.data.get("current_phase")
        fields = ["last_heartbeat", "is_online", "updated_at"]
        if phase:
            signal.current_phase = phase
            fields.append("current_phase")
        signal.save(update_fields=fields)
        return Response({"status": "ok", "controller_id": signal.controller_id})

    @action(detail=False, methods=["get"], url_path="status")
    def status_summary(self, request):
        qs = self.get_queryset()
        return Response(
            {
                "total": qs.count(),
                "online": qs.filter(is_online=True).count(),
                "preempted": qs.filter(is_preempted=True).count(),
                "preemption_capable": qs.filter(supports_preemption=True).count(),
            }
        )


class CameraFeedViewSet(viewsets.ModelViewSet):
    queryset = CameraFeed.objects.select_related("segment", "intersection")
    serializer_class = CameraFeedSerializer
    permission_classes = [PublicReadTrafficWrite]

    @action(detail=True, methods=["post"], permission_classes=[IsTrafficPolice])
    def analyse(self, request, pk=None):
        """Run computer-vision analysis on this camera and ingest the result."""
        camera = self.get_object()
        analysis = analyse_camera(camera)
        observation, events = ingest_camera_analysis(camera, analysis)
        if events:
            broadcast_ops(
                "road_events",
                {"events": RoadEventSerializer(events, many=True).data},
            )
        return Response(
            {
                "camera": camera.name,
                "analysis": analysis.as_dict(),
                "observation_id": observation.id if observation else None,
                "events_created": len(events),
            }
        )


class TrafficObservationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TrafficObservation.objects.select_related("segment")
    serializer_class = TrafficObservationSerializer
    permission_classes = [PublicReadTrafficWrite]

    def get_queryset(self):
        qs = super().get_queryset()
        segment = self.request.query_params.get("segment")
        if segment:
            qs = qs.filter(segment_id=segment)
        return qs


class RoadEventViewSet(viewsets.ModelViewSet):
    queryset = RoadEvent.objects.select_related("segment")
    serializer_class = RoadEventSerializer
    permission_classes = [PublicReadTrafficWrite]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("active", "1") == "1" and self.action == "list":
            qs = qs.active()
        return qs

    def perform_create(self, serializer):
        event = serializer.save()
        broadcast_ops("road_event_created", RoadEventSerializer(event).data)
        # A new blocking event invalidates every route crossing it.
        from apps.brain.rerouting import reassess_active_trips

        reassess_active_trips(reason=f"{event.get_event_type_display()} reported")

    @action(detail=True, methods=["post"], permission_classes=[IsTrafficPolice])
    def clear(self, request, pk=None):
        event = self.get_object()
        event.is_active = False
        event.ends_at = timezone.now()
        event.save(update_fields=["is_active", "ends_at", "updated_at"])
        broadcast_ops("road_event_cleared", {"id": event.id, "uuid": str(event.uuid)})
        return Response({"status": "cleared"})


class AccidentRecordViewSet(viewsets.ModelViewSet):
    queryset = AccidentRecord.objects.all()
    serializer_class = AccidentRecordSerializer
    permission_classes = [PublicReadTrafficWrite]


class AnalyseAllCamerasView(APIView):
    """Sweep every active camera - the CV batch entry point (feature 4.5)."""

    permission_classes = [IsTrafficPolice]

    def post(self, request):
        serializer = CameraAnalysisRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data.get("camera_ids")
        cameras = CameraFeed.objects.filter(is_active=True).select_related("segment")
        if ids:
            cameras = cameras.filter(id__in=ids)

        analysed, events_created = 0, 0
        for camera in cameras:
            analysis = analyse_camera(camera)
            _, events = ingest_camera_analysis(camera, analysis)
            analysed += 1
            events_created += len(events)

        return Response(
            {"cameras_analysed": analysed, "events_created": events_created},
            status=status.HTTP_200_OK,
        )
