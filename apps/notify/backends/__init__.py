"""Push transports.

One interface, three implementations: Web Push (the real one), FCM (optional,
for native Android clients) and a console backend used by tests and by any
deployment that has not configured keys yet.
"""
from __future__ import annotations

from apps.notify.backends.base import PushBackendBase, PushResult
from apps.notify.backends.console import ConsoleBackend
from apps.notify.backends.fcm import FCMBackend
from apps.notify.backends.webpush import WebPushBackend

__all__ = [
    "ConsoleBackend",
    "FCMBackend",
    "PushBackendBase",
    "PushResult",
    "WebPushBackend",
    "get_backend",
]


def get_backend(name: str) -> PushBackendBase:
    """Resolve a backend, falling back to console rather than raising.

    A misconfigured push transport must degrade to "logged but not delivered",
    never to an exception inside a dispatch decision.
    """
    from apps.notify.models import PushBackend as BackendChoice

    if name == BackendChoice.FCM:
        backend: PushBackendBase = FCMBackend()
    else:
        backend = WebPushBackend()
    return backend if backend.is_available() else ConsoleBackend(reason=backend.unavailable_reason())
