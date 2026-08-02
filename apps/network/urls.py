from django.urls import include, path
from apps.core.routers import SEVPSRouter

from apps.network import cv_views, gis_views, views

router = SEVPSRouter()
router.register("intersections", views.IntersectionViewSet, basename="intersection")
router.register("segments", views.RoadSegmentViewSet, basename="segment")
router.register("signals", views.TrafficSignalViewSet, basename="signal")
router.register("cameras", views.CameraFeedViewSet, basename="camera")
router.register("observations", views.TrafficObservationViewSet, basename="observation")
router.register("events", views.RoadEventViewSet, basename="roadevent")
router.register("accidents", views.AccidentRecordViewSet, basename="accident")

urlpatterns = [
    path("vision/analyse-all/", views.AnalyseAllCamerasView.as_view(), name="cv-analyse-all"),
    # --- Phase 7: computer vision ---
    path("cv/status/", cv_views.VisionStatusView.as_view(), name="cv-status"),
    path("cv/cameras/<int:camera_id>/analyse/", cv_views.analyse_one, name="cv-analyse-one"),
    path("cv/sweep/", cv_views.sweep_cameras, name="cv-sweep"),
    path("cv/emergency/", cv_views.emergency_sightings, name="cv-emergency"),
    # --- Phase 8: GIS layers ---
    path("gis/layers/", gis_views.LayerCatalogueView.as_view(), name="gis-catalogue"),
    path("gis/layers/<str:name>/", gis_views.LayerView.as_view(), name="gis-layer"),
    path("gis/basemaps/", gis_views.basemaps, name="gis-basemaps"),
    path("", include(router.urls)),
]
