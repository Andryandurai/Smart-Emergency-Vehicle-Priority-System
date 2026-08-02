"""The backend used when no push transport is configured.

Not a stub for tests only. A pilot install with no VAPID key must still run:
every notification still reaches open dashboards over the WebSocket, still
lands in the durable notification record, and this backend leaves a log line
saying what *would* have been pushed. What must never happen is a missing key
raising inside a dispatch decision.

Deliveries are recorded as SKIPPED with the reason, so ``/notify/health/``
reports "push not configured" rather than "delivering fine, zero reach".
"""
from __future__ import annotations

import logging

from apps.notify.backends.base import PushBackendBase, PushResult

log = logging.getLogger("sevps.notify")


class ConsoleBackend(PushBackendBase):
    name = "console"

    def __init__(self, reason: str = "no push backend configured"):
        self.reason = reason

    def is_available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return self.reason

    def send(self, subscription, payload: dict, *, ttl: int, urgency: str) -> PushResult:
        log.info(
            "push not sent (%s): [%s] %s -> %s",
            self.reason, payload.get("severity"), payload.get("title"), subscription,
        )
        return PushResult.failure(self.reason)
