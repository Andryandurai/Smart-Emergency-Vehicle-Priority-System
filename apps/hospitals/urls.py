from django.urls import include, path
from apps.core.routers import SEVPSRouter

from apps.hospitals import views

router = SEVPSRouter()
router.register("hospitals", views.HospitalViewSet, basename="hospital")
router.register("capabilities", views.HospitalCapabilityViewSet, basename="capability")
router.register("rules", views.EmergencyRuleViewSet, basename="emergencyrule")
router.register("alerts", views.HospitalAlertViewSet, basename="hospitalalert")
router.register(
    "recommendation-logs", views.HospitalRecommendationLogViewSet, basename="recommendationlog"
)

urlpatterns = [
    path("recommend/", views.RecommendHospitalView.as_view(), name="hospital-recommend"),
    path("rule-lookup/<str:category>/", views.RuleLookupView.as_view(), name="hospital-rule-lookup"),
    path("", include(router.urls)),
]
