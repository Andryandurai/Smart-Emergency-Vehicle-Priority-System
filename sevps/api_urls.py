"""Aggregated REST API surface (v1) for every SEVPS layer."""
from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token

from apps.core.auth_views import (
    AccessPolicyView,
    LogoutView,
    RoleCatalogueView,
    WhoAmIView,
)
from apps.core.profile_views import DemoAccountsView, MyAvatarView, MyProfileView
from apps.core.jwt import (
    SEVPSTokenObtainPairView,
    SEVPSTokenRefreshView,
    SEVPSTokenVerifyView,
)
from apps.core.views import HealthView, LivenessView, ReadinessView, ServiceInfoView

#: Authentication. JWT is the target scheme; the legacy DRF token endpoint is
#: retained so existing field devices keep working through the migration and
#: can be cut over on their own schedule.
auth_patterns = [
    path("jwt/create/", SEVPSTokenObtainPairView.as_view(), name="jwt-create"),
    path("jwt/refresh/", SEVPSTokenRefreshView.as_view(), name="jwt-refresh"),
    path("jwt/verify/", SEVPSTokenVerifyView.as_view(), name="jwt-verify"),
    path("jwt/logout/", LogoutView.as_view(), name="jwt-logout"),
    path("me/", WhoAmIView.as_view(), name="auth-me"),
    path("profile/", MyProfileView.as_view(), name="auth-profile"),
    path("profile/avatar/", MyAvatarView.as_view(), name="auth-profile-avatar"),
    path("demo-accounts/", DemoAccountsView.as_view(), name="auth-demo-accounts"),
    path("roles/", RoleCatalogueView.as_view(), name="auth-roles"),
    path("policy/", AccessPolicyView.as_view(), name="auth-policy"),
    # Deprecated: legacy DRF token issuance. Scheduled for removal once all
    # clients report a JWT auth_method on /auth/me/.
    path("token/", obtain_auth_token, name="api-token"),
]

urlpatterns = [
    path("health/", HealthView.as_view(), name="api-health"),
    # Split probes (Phase 11). Liveness must not check dependencies; see the
    # views for why restarting on a database outage makes recovery worse.
    path("health/live/", LivenessView.as_view(), name="api-health-live"),
    path("health/ready/", ReadinessView.as_view(), name="api-health-ready"),
    path("info/", ServiceInfoView.as_view(), name="api-info"),
    path("auth/", include(auth_patterns)),
    path("fleet/", include("apps.fleet.urls")),
    path("network/", include("apps.network.urls")),
    path("brain/", include("apps.brain.urls")),
    path("hospitals/", include("apps.hospitals.urls")),
    path("dispatch/", include("apps.dispatch.urls")),
    path("alerts/", include("apps.alerts.urls")),
    path("analytics/", include("apps.analytics.urls")),
    path("notify/", include("apps.notify.urls")),
]
