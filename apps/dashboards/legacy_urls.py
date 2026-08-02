"""Server-rendered screens, retained under /legacy/.

These are not dead code. They are the fallback for kiosk and embedded displays
with no JavaScript build pipeline, and they keep SEVPS usable when the React
bundle has not been built. Phase 4 replaced them as the *primary* console; it
did not remove the capability.
"""
from django.contrib.auth import views as auth_views
from django.urls import path

from apps.dashboards import views

urlpatterns = [
    # Django session sign-in. Used by the legacy screens and as the admin's
    # LOGIN_URL; the React console authenticates with JWT instead.
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="dashboards/login.html", redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(next_page="/legacy/"), name="logout"),
    path("", views.operations, name="legacy-operations"),
    path("hospitals/", views.hospital_index, name="legacy-hospital-index"),
    path("hospital/<str:code>/", views.hospital_dashboard, name="legacy-hospital"),
    path("analytics/", views.analytics, name="legacy-analytics"),
    path("paramedic/", views.paramedic, name="legacy-paramedic-index"),
    path("paramedic/<str:callsign>/", views.paramedic, name="legacy-paramedic"),
    path("driver/", views.driver_view, name="legacy-driver"),
    path("boards/", views.board_wall, name="legacy-boards"),
]
