"""JWT authentication for the Channels (WebSocket) layer.

Channels' stock ``AuthMiddlewareStack`` authenticates from the **session
cookie**. Once the REST API moves to JWT, a browser that authenticated purely
with a bearer token has no session - so every socket silently connects as
``AnonymousUser`` while the REST calls beside it work fine. That mismatch is
the classic failure mode of a JWT migration, and it fails *open* here because
the consumers were written before roles existed.

This middleware closes it. Resolution order:

1. ``Authorization: Bearer <token>`` header - native/field clients that can
   set headers on the upgrade request.
2. ``?token=<jwt>`` query parameter - browsers, which cannot set headers on
   the WebSocket handshake. Kept last and documented as a trade-off: query
   strings land in access logs, so tokens are short-lived by design.
3. Session cookie, via the wrapped stack - preserves the existing
   server-rendered dashboards during the migration.

An invalid or expired token yields ``AnonymousUser`` rather than an error:
the consumer decides whether anonymous is acceptable for that endpoint, which
keeps public feeds (display boards) working while protected ones can refuse.
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser

log = logging.getLogger("sevps.ws.auth")


@database_sync_to_async
def _user_from_token(raw_token: str):
    """Validate a JWT and return its user, or AnonymousUser."""
    from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
    from rest_framework_simplejwt.tokens import UntypedToken
    from django.contrib.auth import get_user_model

    try:
        validated = UntypedToken(raw_token)
    except (InvalidToken, TokenError) as exc:
        log.debug("rejected websocket token: %s", exc)
        return AnonymousUser()

    user_id = validated.get("user_id")
    if user_id is None:
        return AnonymousUser()

    User = get_user_model()
    try:
        # select_related is pointless here, but prefetching groups saves a
        # query on every permission check the consumer subsequently makes.
        return User.objects.prefetch_related("groups").get(pk=user_id, is_active=True)
    except User.DoesNotExist:
        return AnonymousUser()


def _extract_token(scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            decoded = value.decode(errors="replace")
            if decoded.lower().startswith("bearer "):
                return decoded[7:].strip()

    query = parse_qs(scope.get("query_string", b"").decode(errors="replace"))
    token = query.get("token") or query.get("access_token")
    return token[0] if token else None


class JWTAuthMiddleware:
    """Populates ``scope['user']`` from a JWT when one is present."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        token = _extract_token(scope)
        if token:
            scope = dict(scope)
            scope["user"] = await _user_from_token(token)
            scope["auth_method"] = "jwt"
        return await self.inner(scope, receive, send)


def JWTAuthMiddlewareStack(inner):
    """Session stack on the outside, JWT on the inside. Order matters.

    Channels' ``AuthMiddleware`` resolves ``scope['user']`` *before* handing
    off to whatever it wraps, so it must run first; the JWT layer then
    overrides the identity only when a token is actually present. Nesting
    these the other way round lets the session layer clobber the JWT user
    with ``AnonymousUser`` - which fails open, and silently.

    The result is that both auth styles work simultaneously: dashboards that
    signed in at ``/login/`` keep their sockets, and JWT clients are
    authenticated on the same endpoints.
    """
    return AuthMiddlewareStack(JWTAuthMiddleware(inner))
