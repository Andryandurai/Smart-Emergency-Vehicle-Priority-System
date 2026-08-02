from django.urls import include, path
from apps.core.routers import SEVPSRouter

from apps.network import views

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
    path("", include(router.urls)),
]
