"""Southbound adapters to real traffic signal controllers (Layer 3).

Cities expose their controllers differently - a vendor REST API, an NTCIP
bridge, or nothing at all during a pilot.  SEVPS keeps that variation at the
edge: everything above this module works with :class:`ControllerResult`, and
adding a new city means adding one adapter class here.

Every adapter is expected to be *fail-safe*, never fail-open: if a controller
cannot be reached, the preemption is marked failed and the corridor planner
routes around the assumption that the light will be green.  A signal SEVPS
cannot talk to is simply a signal with normal timing, which is safe.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

import requests
from django.utils import timezone

from apps.core.enums import SignalPhase
from apps.core.realtime import GROUP_SIGNALS, broadcast, broadcast_ops

log = logging.getLogger("sevps.dispatch.controllers")

REQUEST_TIMEOUT_S = 3.0


def _jsonable(value):
    """Coerce a value into something a JSONField can store.

    Controller payloads carry timestamps and model ids straight from the
    dispatch context; they are persisted on the preemption record for audit,
    so they have to survive ``json.dumps`` without a custom encoder.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


@dataclass
class ControllerResult:
    ok: bool
    detail: str = ""
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """JSON-safe form - persisted verbatim on the preemption record."""
        return {"ok": self.ok, "detail": self.detail, **_jsonable(self.payload)}


class BaseSignalController:
    """Interface every controller adapter implements."""

    key = "base"

    def request_green(self, signal, *, duration_s: float, context: dict) -> ControllerResult:
        raise NotImplementedError

    def release(self, signal, *, context: dict) -> ControllerResult:
        raise NotImplementedError

    # Shared bookkeeping so adapters only implement the transport.
    def _mark_preempted(self, signal, duration_s: float) -> None:
        now = timezone.now()
        signal.is_preempted = True
        signal.current_phase = SignalPhase.GREEN
        signal.preempted_until = now + timezone.timedelta(seconds=duration_s)
        signal.last_preempted_at = now
        signal.save(
            update_fields=[
                "is_preempted", "current_phase", "preempted_until",
                "last_preempted_at", "updated_at",
            ]
        )

    def _mark_released(self, signal) -> None:
        signal.is_preempted = False
        signal.current_phase = SignalPhase.RED
        signal.preempted_until = None
        signal.save(
            update_fields=["is_preempted", "current_phase", "preempted_until", "updated_at"]
        )


class SimulatedSignalController(BaseSignalController):
    """In-platform controller used for pilots, demos and tests.

    Applies the state change to the database and announces it on the signals
    channel, which is what a hardware bridge would subscribe to.
    """

    key = "simulated"

    def request_green(self, signal, *, duration_s: float, context: dict) -> ControllerResult:
        self._mark_preempted(signal, duration_s)
        payload = {
            "command": "request_green",
            "controller_id": signal.controller_id,
            "intersection_id": signal.intersection_id,
            "duration_s": round(duration_s, 1),
            "until": signal.preempted_until,
            **context,
        }
        broadcast(GROUP_SIGNALS, "signal_command", payload)
        broadcast_ops("signal_preempted", payload)
        return ControllerResult(True, "simulated green granted", payload)

    def release(self, signal, *, context: dict) -> ControllerResult:
        self._mark_released(signal)
        payload = {
            "command": "release",
            "controller_id": signal.controller_id,
            "intersection_id": signal.intersection_id,
            **context,
        }
        broadcast(GROUP_SIGNALS, "signal_command", payload)
        broadcast_ops("signal_released", payload)
        return ControllerResult(True, "simulated normal timing restored", payload)


class HttpSignalController(BaseSignalController):
    """Generic REST adapter for vendor controllers.

    Expects ``POST {endpoint}/preempt`` and ``POST {endpoint}/release``.
    Timeouts are short and failures are swallowed into a failed result -
    dispatch must never block on a traffic cabinet.
    """

    key = "http"

    def _post(self, signal, path: str, body: dict) -> ControllerResult:
        if not signal.endpoint:
            return ControllerResult(False, "controller has no endpoint configured")
        url = f"{signal.endpoint.rstrip('/')}/{path}"
        try:
            response = requests.post(url, json=body, timeout=REQUEST_TIMEOUT_S)
            response.raise_for_status()
        except requests.RequestException as exc:
            log.warning("controller %s unreachable: %s", signal.controller_id, exc)
            signal.is_online = False
            signal.save(update_fields=["is_online", "updated_at"])
            return ControllerResult(False, f"controller unreachable: {exc}")

        try:
            payload = response.json()
        except ValueError:
            payload = {"raw": response.text[:500]}
        return ControllerResult(True, "controller accepted", payload)

    def request_green(self, signal, *, duration_s: float, context: dict) -> ControllerResult:
        result = self._post(
            signal,
            "preempt",
            {
                "controller_id": signal.controller_id,
                "duration_s": round(duration_s, 1),
                "approach": context.get("approach"),
                "priority_level": context.get("priority_level"),
                "trip_reference": context.get("trip_reference"),
            },
        )
        if result.ok:
            self._mark_preempted(signal, duration_s)
            broadcast_ops(
                "signal_preempted",
                {"controller_id": signal.controller_id, "duration_s": duration_s, **context},
            )
        return result

    def release(self, signal, *, context: dict) -> ControllerResult:
        result = self._post(signal, "release", {"controller_id": signal.controller_id})
        if result.ok:
            self._mark_released(signal)
            broadcast_ops("signal_released", {"controller_id": signal.controller_id, **context})
        return result


class NullSignalController(BaseSignalController):
    """For intersections SEVPS may observe but must not command.

    Used where a junction is under manual police control or the authority has
    not yet granted write access.  The corridor is planned around it.
    """

    key = "null"

    def request_green(self, signal, *, duration_s: float, context: dict) -> ControllerResult:
        return ControllerResult(False, "controller is read-only (no preemption authority)")

    def release(self, signal, *, context: dict) -> ControllerResult:
        return ControllerResult(True, "nothing to release")


_REGISTRY: dict[str, BaseSignalController] = {
    SimulatedSignalController.key: SimulatedSignalController(),
    HttpSignalController.key: HttpSignalController(),
    NullSignalController.key: NullSignalController(),
}


def get_controller(signal) -> BaseSignalController:
    """Resolve the adapter for a signal, defaulting to the simulator."""
    return _REGISTRY.get(signal.controller_type, _REGISTRY[SimulatedSignalController.key])


def register_controller(controller: BaseSignalController) -> None:
    """Hook for city-specific adapters added by an integrator."""
    _REGISTRY[controller.key] = controller
