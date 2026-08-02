"""Firebase Cloud Messaging — optional, for native Android clients.

Why this is the adapter and not the default
-------------------------------------------
The brief listed FCM first. The recommendation, stated in Phase 1 and
implemented here, is to keep it but demote it, for three reasons:

1. **It puts a single vendor in the delivery path of emergency alerts.** Web
   Push spreads that across whichever push service each browser already uses.
   An FCM outage or policy change is one point of failure for every user.
2. **It duplicates capability SEVPS already has.** For browser clients FCM is a
   wrapper over the same Web Push transport, plus an SDK, plus a Google Cloud
   project. Nothing is gained for the browser dashboards that are the platform.
3. **It requires an account, a project and a service-account key** before a
   notification can be sent at all. A pilot deployment can run Web Push with
   one `manage.py generate_vapid_keys`.

Where FCM genuinely wins is the case Web Push cannot serve: a **native Android
driver app**, which holds an FCM registration token rather than a W3C
subscription. That is a real target for the Layer 4 driver alerts, so the
adapter exists and shares the delivery, retry and receipt machinery — set
``SEVPS_FCM_CREDENTIALS`` and native tokens deliver alongside browsers with no
other change.

**Recommendation: integrate, do not replace.** Web Push stays primary.
"""
from __future__ import annotations

import logging
import time

from django.conf import settings

from apps.notify.backends.base import PushBackendBase, PushResult

log = logging.getLogger("sevps.notify")

_firebase_app = None


def _load_app():
    """Initialise the Firebase app once, lazily.

    Lazily because the overwhelmingly common deployment has no FCM credentials
    and must not pay an import or a network call for a transport it never uses.
    """
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    credentials_path = settings.SEVPS.get("FCM_CREDENTIALS", "")
    if not credentials_path:
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials as fb_credentials
    except ImportError:
        log.warning("SEVPS_FCM_CREDENTIALS is set but firebase-admin is not installed")
        return None
    try:
        _firebase_app = firebase_admin.initialize_app(
            fb_credentials.Certificate(credentials_path), name="sevps"
        )
    except ValueError:  # already initialised in this process
        _firebase_app = firebase_admin.get_app("sevps")
    except Exception:
        log.exception("FCM initialisation failed; falling back to Web Push only")
        return None
    return _firebase_app


class FCMBackend(PushBackendBase):
    name = "fcm"

    def is_available(self) -> bool:
        return _load_app() is not None

    def unavailable_reason(self) -> str:
        if not settings.SEVPS.get("FCM_CREDENTIALS", ""):
            return "FCM not configured (SEVPS_FCM_CREDENTIALS unset) - this is the normal case"
        return "FCM credentials set but firebase-admin unavailable or invalid"

    def send(self, subscription, payload: dict, *, ttl: int, urgency: str) -> PushResult:
        app = _load_app()
        if app is None:
            return PushResult.failure("FCM not configured")

        from firebase_admin import messaging

        started = time.monotonic()
        message = messaging.Message(
            token=subscription.endpoint,  # FCM registration token
            # Data-only: the client renders the notification so it can apply
            # the same dedupe_key collapsing the service worker uses. A
            # `notification` block would let the OS draw three separate banners
            # for one rerouted trip.
            data={
                "title": payload.get("title", ""),
                "body": payload.get("body", ""),
                "severity": payload.get("severity", "info"),
                "category": payload.get("category", ""),
                "link": payload.get("link", ""),
                "dedupe_key": payload.get("dedupe_key", ""),
                "id": str(payload.get("id", "")),
            },
            android=messaging.AndroidConfig(
                priority="high" if urgency in ("high", "very-low") else "normal",
                ttl=ttl,
                collapse_key=payload.get("dedupe_key") or None,
            ),
        )
        try:
            messaging.send(message, app=app)
        except Exception as exc:
            gone = type(exc).__name__ in (
                "UnregisteredError", "SenderIdMismatchError", "InvalidArgumentError",
            )
            return PushResult.failure(f"{type(exc).__name__}: {exc}", gone=gone)

        return PushResult.success(200, int((time.monotonic() - started) * 1000))
