"""WSGI entrypoint (HTTP only - use asgi.py to keep WebSockets working)."""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sevps.settings")

application = get_wsgi_application()
