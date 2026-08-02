from django.urls import path

from apps.brain import ml_views, views

urlpatterns = [
    path("route/", views.RouteView.as_view(), name="brain-route"),
    path("route/compare/", views.CompareAlgorithmsView.as_view(), name="brain-route-compare"),
    path("congestion/forecast/", views.CongestionForecastView.as_view(), name="brain-forecast"),
    path("network/summary/", views.network_summary, name="brain-network-summary"),
    path("network/rebuild/", views.rebuild_graph, name="brain-network-rebuild"),
    path("priority/ranking/", views.priority_ranking, name="brain-priority-ranking"),
    path("corridor/<int:trip_id>/preview/", views.corridor_preview, name="brain-corridor-preview"),
    path("reroute/<int:trip_id>/evaluate/", views.evaluate_reroute, name="brain-reroute-evaluate"),
    path("reroute/reassess/", views.reassess_all, name="brain-reassess"),
    # --- Phase 6: predictions, each with confidence and explanation ---
    path("ml/models/", ml_views.ModelStatusView.as_view(), name="ml-models"),
    path("ml/congestion/", ml_views.CongestionPredictionView.as_view(), name="ml-congestion"),
    path("ml/eta/<int:trip_id>/", ml_views.predict_trip_eta, name="ml-eta"),
    path("ml/priority/", ml_views.PriorityPredictionView.as_view(), name="ml-priority"),
    path("ml/corridor/<int:signal_id>/", ml_views.predict_corridor, name="ml-corridor"),
    path("ml/hospital/", ml_views.ExplainedRecommendationView.as_view(), name="ml-hospital"),
]
