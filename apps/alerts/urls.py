from django.urls import include, path
from apps.core.routers import SEVPSRouter

from apps.alerts import views

router = SEVPSRouter()
router.register("boards", views.DisplayBoardViewSet, basename="displayboard")
router.register("devices", views.DriverDeviceViewSet, basename="driverdevice")
router.register("driver-alerts", views.DriverAlertViewSet, basename="driveralert")

urlpatterns = [
    path("position/", views.DevicePositionView.as_view(), name="alerts-position"),
    path("nearby/", views.NearbyAlertsView.as_view(), name="alerts-nearby"),
    path("", include(router.urls)),
]
