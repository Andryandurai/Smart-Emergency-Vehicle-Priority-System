"""Identity endpoints: who am I, what may I do, and how do I sign out."""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api_policy import policy_summary
from apps.core.jwt import REFRESH_COOKIE
from apps.core.permissions import IsAdministrator, role_summary
from apps.core.roles import describe_roles


class WhoAmIView(APIView):
    """``GET /api/v1/auth/me/`` - identity and capabilities for the caller.

    The React app calls this once on boot to decide which screens to render.
    It deliberately re-derives roles from the database rather than reading the
    JWT claim, so a role revoked mid-session takes effect on the next call.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        return Response(
            {
                "id": user.id,
                "username": user.get_username(),
                "email": user.email,
                "name": user.get_full_name(),
                "auth_method": self._auth_method(request),
                **role_summary(user),
            }
        )

    @staticmethod
    def _auth_method(request) -> str:
        authenticator = request.successful_authenticator
        return {
            "JWTAuthentication": "jwt",
            "TokenAuthentication": "legacy_token",
            "SessionAuthentication": "session",
        }.get(type(authenticator).__name__, "unknown")


class LogoutView(APIView):
    """``POST /api/v1/auth/jwt/logout/`` - blacklist the refresh token.

    Access tokens are stateless and cannot be revoked before they expire;
    that is why they are short-lived. Blacklisting the refresh token is what
    actually ends the session, and clearing the cookie stops the browser
    silently re-authenticating.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.tokens import RefreshToken

        raw = request.data.get("refresh") or request.COOKIES.get(REFRESH_COOKIE)
        detail = "no refresh token supplied; cookie cleared"
        if raw:
            try:
                RefreshToken(raw).blacklist()
                detail = "refresh token blacklisted"
            except TokenError:
                detail = "refresh token already expired or invalid"

        response = Response({"detail": detail}, status=status.HTTP_200_OK)
        response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth/")
        return response


class RoleCatalogueView(APIView):
    """``GET /api/v1/auth/roles/`` - the six roles and what each may do."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"roles": describe_roles()})


class AccessPolicyView(APIView):
    """``GET /api/v1/auth/policy/`` - live audit of endpoint protection.

    Administrators only: it enumerates the API surface, which is useful to a
    reviewer and equally useful to an attacker.
    """

    permission_classes = [IsAdministrator]

    def get(self, request):
        from apps.core.ws_policy import policy_summary as ws_policy_summary

        return Response({"rest": policy_summary(), "websockets": ws_policy_summary()})
