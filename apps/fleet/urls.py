from django.urls import include, path
from apps.core.routers import SEVPSRouter

from apps.fleet import crew_api, driver_api, views

router = SEVPSRouter()
router.register("vehicles", views.EmergencyVehicleViewSet, basename="vehicle")
router.register("stations", views.StationViewSet, basename="station")
router.register("shifts", crew_api.CrewShiftViewSet, basename="crewshift")
router.register("maintenance", driver_api.MaintenanceViewSet, basename="maintenance")
router.register("breakdowns", driver_api.BreakdownViewSet, basename="breakdown")

urlpatterns = [
    path("board/", driver_api.FleetBoardView.as_view(), name="fleet-board"),
    path(
        "vehicles/<str:callsign>/readiness/",
        driver_api.OverrideReadinessView.as_view(),
        name="fleet-readiness-override",
    ),
    path("", include(router.urls)),
]
