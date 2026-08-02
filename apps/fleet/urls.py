from django.urls import include, path
from apps.core.routers import SEVPSRouter

from apps.fleet import views

router = SEVPSRouter()
router.register("vehicles", views.EmergencyVehicleViewSet, basename="vehicle")
router.register("stations", views.StationViewSet, basename="station")

urlpatterns = [path("", include(router.urls))]
