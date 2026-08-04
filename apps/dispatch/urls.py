from django.urls import include, path
from apps.core.routers import SEVPSRouter

from apps.dispatch import views

router = SEVPSRouter()
router.register("trips", views.EmergencyTripViewSet, basename="trip")
router.register("routes", views.RoutePlanViewSet, basename="routeplan")
router.register("preemptions", views.SignalPreemptionViewSet, basename="preemption")
router.register("directives", views.PriorityDirectiveViewSet, basename="directive")

urlpatterns = [
    path("priority-profiles/", views.priority_profiles, name="dispatch-priority-profiles"),
    path("corridor/tick/", views.corridor_tick, name="dispatch-corridor-tick"),
    path("journeys/tick/", views.journey_tick, name="dispatch-journey-tick"),
    path("", include(router.urls)),
]
