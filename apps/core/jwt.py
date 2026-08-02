"""JWT issuance for SEVPS.

Two decisions worth stating, because both are security-relevant:

**Roles are embedded as a claim.** A dashboard needs to know what to render
before it can render anything. Without role claims every client pays an extra
round trip on load, and worse, tends to cache the answer somewhere sloppy.
The claim is advisory for UI only - the server re-derives roles from the
database on every request and never trusts the token's copy for authorisation.

**The refresh token goes in an httpOnly cookie, the access token does not.**
An access token held in JavaScript memory dies with the tab; a refresh token
in ``localStorage`` is readable by any XSS and is a persistent account
takeover. Clients that cannot use cookies (native apps, field devices) may
still take the refresh token from the response body.
"""
from __future__ import annotations

from django.conf import settings
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer,
    TokenRefreshSerializer,
)
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from apps.core.roles import user_roles

REFRESH_COOKIE = "sevps_refresh"


class SEVPSTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Adds identity and role claims for client-side rendering."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["username"] = user.get_username()
        token["roles"] = sorted(user_roles(user))
        token["is_staff"] = user.is_staff
        token["is_superuser"] = user.is_superuser
        full_name = user.get_full_name()
        if full_name:
            token["name"] = full_name
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        from apps.core.permissions import role_summary

        data["user"] = {
            "id": self.user.id,
            "username": self.user.get_username(),
            "email": self.user.email,
            "name": self.user.get_full_name(),
            **role_summary(self.user),
        }
        return data


def _cookie_kwargs() -> dict:
    """Cookie flags. Secure is tied to DEBUG so local HTTP still works."""
    return {
        "httponly": True,
        "secure": not settings.DEBUG,
        "samesite": "Lax",
        "path": "/api/v1/auth/",
        "max_age": int(
            settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()
        ),
    }


class _RefreshCookieMixin:
    """Mirrors the refresh token into an httpOnly cookie on every issue."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        refresh = getattr(response, "data", None) and response.data.get("refresh")
        if refresh:
            response.set_cookie(REFRESH_COOKIE, refresh, **_cookie_kwargs())
        return response


class SEVPSTokenObtainPairView(_RefreshCookieMixin, TokenObtainPairView):
    """``POST /api/v1/auth/jwt/create/`` - username + password -> token pair."""

    # Declared rather than inherited: the policy audit requires every view
    # to state its own access policy. Credentials are the payload here.
    permission_classes = [AllowAny]
    serializer_class = SEVPSTokenObtainPairSerializer


class CookieTokenRefreshSerializer(TokenRefreshSerializer):
    """Accepts the refresh token from the body *or* the httpOnly cookie."""

    refresh = None  # made optional; sourced from the cookie when absent

    def validate(self, attrs):
        attrs["refresh"] = self.context["request"].data.get("refresh") or (
            self.context["request"].COOKIES.get(REFRESH_COOKIE)
        )
        if not attrs["refresh"]:
            from rest_framework import serializers

            raise serializers.ValidationError(
                {"refresh": "No refresh token supplied in body or cookie."}
            )
        return super().validate(attrs)


class SEVPSTokenRefreshView(_RefreshCookieMixin, TokenRefreshView):
    """``POST /api/v1/auth/jwt/refresh/`` - rotate the pair."""

    permission_classes = [AllowAny]
    serializer_class = CookieTokenRefreshSerializer


class SEVPSTokenVerifyView(TokenVerifyView):
    """``POST /api/v1/auth/jwt/verify/`` - is this token still valid?"""

    permission_classes = [AllowAny]
