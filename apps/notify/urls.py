"""Notification and push endpoints (Phase 9)."""
from django.urls import path

from apps.notify import views

urlpatterns = [
    path("vapid-key/", views.VapidPublicKeyView.as_view(), name="notify-vapid-key"),
    path("subscribe/", views.SubscribeView.as_view(), name="notify-subscribe"),
    path("unsubscribe/", views.UnsubscribeView.as_view(), name="notify-unsubscribe"),
    path("subscriptions/", views.MySubscriptionsView.as_view(), name="notify-subscriptions"),
    path("preferences/", views.PreferenceView.as_view(), name="notify-preferences"),
    path("inbox/", views.InboxView.as_view(), name="notify-inbox"),
    path("read/", views.mark_read, name="notify-read-all"),
    path("read/<uuid:uuid>/", views.mark_read, name="notify-read"),
    path("test/", views.send_test, name="notify-test"),
    path("health/", views.PushHealthView.as_view(), name="notify-health"),
]
