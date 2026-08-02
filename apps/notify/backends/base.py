"""The contract every push transport implements."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PushResult:
    """Outcome of one delivery attempt.

    ``gone`` is separated from ``ok`` because it means something specific and
    actionable: the push service says this endpoint no longer exists (HTTP 404
    or 410), which happens when a user clears site data or uninstalls. That is
    authoritative — the subscription is retired immediately rather than
    retried, which is what keeps the subscription table from filling with
    endpoints that will never work again.
    """

    ok: bool
    status_code: int | None = None
    detail: str = ""
    gone: bool = False
    latency_ms: int | None = None

    @classmethod
    def success(cls, status_code: int = 201, latency_ms: int | None = None) -> "PushResult":
        return cls(ok=True, status_code=status_code, latency_ms=latency_ms)

    @classmethod
    def failure(cls, detail: str, status_code: int | None = None, *, gone: bool = False):
        return cls(ok=False, status_code=status_code, detail=detail[:250], gone=gone)


class PushBackendBase:
    """A transport that can deliver one payload to one subscription."""

    name = "base"

    def is_available(self) -> bool:
        raise NotImplementedError

    def unavailable_reason(self) -> str:
        return ""

    def send(self, subscription, payload: dict, *, ttl: int, urgency: str) -> PushResult:
        raise NotImplementedError
