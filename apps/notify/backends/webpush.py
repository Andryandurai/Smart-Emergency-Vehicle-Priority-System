"""Web Push over VAPID — the primary transport.

Chosen over FCM as the default because Web Push is a W3C/IETF standard
delivered by whichever push service the *browser* already uses (Mozilla's for
Firefox, Apple's for Safari, Google's for Chrome). SEVPS needs no account, no
SDK and no vendor relationship, and a policy change at any single vendor cannot
take emergency alerting offline for every user at once.

Payloads are encrypted client-side-bound (RFC 8291): the push service relays
ciphertext it cannot read. That matters here — the relay is a third party and
the messages concern medical emergencies.
"""
from __future__ import annotations

import json
import logging
import time

from apps.notify import vapid
from apps.notify.backends.base import PushBackendBase, PushResult

log = logging.getLogger("sevps.notify")

#: How long a push service should hold an undelivered message. Emergency
#: notifications are worthless late: a "corridor failed" arriving twenty
#: minutes after the ambulance passed the junction is noise that trains people
#: to ignore the channel. Critical messages get an even shorter TTL.
DEFAULT_TTL_S = 600
CRITICAL_TTL_S = 180

try:  # pragma: no cover - import guard
    from pywebpush import WebPushException, webpush

    _AVAILABLE = True
except ImportError:  # pragma: no cover
    webpush = None  # type: ignore[assignment]
    WebPushException = Exception  # type: ignore[misc,assignment]
    _AVAILABLE = False


class WebPushBackend(PushBackendBase):
    name = "webpush"

    def is_available(self) -> bool:
        # The subject is part of availability, not a runtime detail: a push
        # service rejects a VAPID JWT without a contactable `sub`, so a
        # deployment missing it would fail every single send with a confusing
        # 403. Better to report "not configured" up front.
        return _AVAILABLE and vapid.is_configured() and vapid.claims_configured()

    def unavailable_reason(self) -> str:
        if not _AVAILABLE:
            return "pywebpush is not installed"
        if not vapid.is_configured():
            return "no VAPID key: run `manage.py generate_vapid_keys`"
        if not vapid.claims_configured():
            return "SEVPS_VAPID_SUBJECT is not set (mailto: or https: URI)"
        return ""

    def send(self, subscription, payload: dict, *, ttl: int, urgency: str) -> PushResult:
        info = {
            "endpoint": subscription.endpoint,
            "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
        }
        started = time.monotonic()
        try:
            response = webpush(
                subscription_info=info,
                data=json.dumps(payload),
                vapid_private_key=vapid.private_key_pem(),
                vapid_claims=dict(vapid.claims()),
                ttl=ttl,
                headers={"Urgency": urgency},
                timeout=REQUEST_TIMEOUT_S,
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            # 404/410 is the spec's way of saying the subscription is dead.
            return PushResult.failure(str(exc), status, gone=status in (404, 410))
        except Exception as exc:  # network error, DNS, TLS - transient
            return PushResult.failure(f"{type(exc).__name__}: {exc}")

        elapsed = int((time.monotonic() - started) * 1000)
        return PushResult.success(getattr(response, "status_code", 201), elapsed)


#: Bounded because the fan-out is synchronous. Ten dead endpoints at the
#: default socket timeout would stall a notification for minutes.
REQUEST_TIMEOUT_S = 6
