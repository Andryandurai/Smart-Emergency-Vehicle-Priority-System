from django.urls import include, path
from apps.core.routers import SEVPSRouter

from apps.analytics import views

router = SEVPSRouter()
router.register("daily-metrics", views.DailyMetricViewSet, basename="dailymetric")
router.register("hotspots", views.HotspotViewSet, basename="hotspot")

urlpatterns = [
    path("summary/", views.summary, name="analytics-summary"),
    path("response-times/", views.response_times, name="analytics-response-times"),
    path("corridors/", views.corridors, name="analytics-corridors"),
    path("congestion/", views.congestion, name="analytics-congestion"),
    path("delays/", views.delays, name="analytics-delays"),
    path("movement/", views.movement, name="analytics-movement"),
    path("accident-hotspots/", views.accident_hotspots, name="analytics-accident-hotspots"),
    path(
        "accident-hotspots/recompute/",
        views.recompute_accident_hotspots,
        name="analytics-accident-hotspots-recompute",
    ),
    path("rollup/", views.rollup, name="analytics-rollup"),
    path("", include(router.urls)),
]
