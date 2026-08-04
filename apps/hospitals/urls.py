from django.urls import include, path
from apps.core.routers import SEVPSRouter

from apps.hospitals import portal, views

router = SEVPSRouter()
router.register("hospitals", views.HospitalViewSet, basename="hospital")
router.register("capabilities", views.HospitalCapabilityViewSet, basename="capability")
router.register("rules", views.EmergencyRuleViewSet, basename="emergencyrule")
router.register("alerts", views.HospitalAlertViewSet, basename="hospitalalert")
router.register(
    "recommendation-logs", views.HospitalRecommendationLogViewSet, basename="recommendationlog"
)

urlpatterns = [
    # The receiving hospital's own console. Registered before the router so
    # `portal/...` can never be swallowed by the hospitals detail route.
    path("portal/dashboard/", portal.hospital_dashboard, name="hospital-portal-dashboard"),
    path("portal/ambulances/", portal.hospital_ambulances, name="hospital-portal-ambulances"),
    path("portal/update/", portal.hospital_update, name="hospital-portal-update"),
    path("portal/choices/", portal.hospital_choices, name="hospital-portal-choices"),
    path(
        "portal/trips/<int:trip_id>/received/",
        portal.patient_received,
        name="hospital-portal-received",
    ),
    path(
        "portal/trips/<int:trip_id>/admit/",
        portal.admit_patient,
        name="hospital-portal-admit",
    ),
    path("recommend/", views.RecommendHospitalView.as_view(), name="hospital-recommend"),
    path("rule-lookup/<str:category>/", views.RuleLookupView.as_view(), name="hospital-rule-lookup"),
    path("symptoms/", views.SymptomCatalogueView.as_view(), name="hospital-symptoms"),
    path("", include(router.urls)),
]
