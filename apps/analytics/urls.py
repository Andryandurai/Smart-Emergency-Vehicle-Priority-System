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
    # --- Phase 10: chart-shaped series, profiles and exports ---------------
    path("trends/", views.daily_trends, name="analytics-trends"),
    path("trends/summary/", views.trend_summary, name="analytics-trend-summary"),
    path("demand/", views.demand_profile, name="analytics-demand"),
    path("distribution/", views.distribution, name="analytics-distribution"),
    path("corridor-outcomes/", views.corridor_outcomes, name="analytics-corridor-outcomes"),
    path(
        "response-distribution/",
        views.response_distribution,
        name="analytics-response-distribution",
    ),
    path("hospital-load/", views.hospital_load, name="analytics-hospital-load"),
    path("export/", views.export_catalogue, name="analytics-export-catalogue"),
    path("export/<str:dataset>.csv", views.export_csv, name="analytics-export"),
    path("", include(router.urls)),
]
