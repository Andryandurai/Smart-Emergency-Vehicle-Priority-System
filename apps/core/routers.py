"""Router whose auto-generated index declares its own access policy.

DRF's ``DefaultRouter`` synthesises an ``APIRootView`` that lists the routes
it registered. Because that view is generated rather than written, it never
appears in code review and silently inherits whatever the global default
happens to be - which is exactly the class of gap
:mod:`apps.core.api_policy` exists to catch.

Subclassing it means the index has a policy someone chose, and the audit can
stay strict instead of carrying an exemption for framework internals.
"""
from rest_framework.routers import APIRootView, DefaultRouter

from apps.core.permissions import PublicRead


class SEVPSAPIRootView(APIRootView):
    """Route index. Publishes endpoint names only - never any data."""

    permission_classes = [PublicRead]


class SEVPSRouter(DefaultRouter):
    APIRootView = SEVPSAPIRootView
