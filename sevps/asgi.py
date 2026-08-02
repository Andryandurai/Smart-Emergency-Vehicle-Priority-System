"""ASGI entrypoint - serves HTTP and the SEVPS real-time WebSocket layer."""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sevps.settings")

# The HTTP application must be built before importing anything that touches
# the ORM (consumers import models transitively).
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.core.ws_auth import JWTAuthMiddlewareStack  # noqa: E402
from sevps.routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # Accepts both a Django session (server-rendered dashboards) and a
        # JWT (API clients, React frontend). See apps/core/ws_auth.py for why
        # the nesting order matters.
        "websocket": JWTAuthMiddlewareStack(URLRouter(websocket_urlpatterns)),
    }
)
