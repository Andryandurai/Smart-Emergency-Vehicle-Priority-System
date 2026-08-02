"""Thin wrapper over the Channels layer.

Every SEVPS layer publishes through :func:`broadcast`, so swapping the
in-memory layer for Redis (multi-worker deployment) is a config change and
nothing else.  Publishing is deliberately fail-soft: a broken WebSocket fan-out
must never abort a dispatch decision.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.core.geo import geohash

log = logging.getLogger("sevps.realtime")

GROUP_OPS = "ops"
GROUP_SIGNALS = "signals"


def hospital_group(code: str) -> str:
    return f"hospital.{code}".lower()


def vehicle_group(callsign: str) -> str:
    return f"vehicle.{callsign}".lower().replace(" ", "-")


def driver_group(lat: float, lon: float) -> str:
    """Drivers are sharded into ~1.2 km geohash cells (see core.geo)."""
    return f"drivers.{geohash(lat, lon, 6)}"


def broadcast(group: str, event_type: str, payload: dict[str, Any]) -> None:
    """Publish one event to a channel group. Never raises."""
    layer = get_channel_layer()
    if layer is None:  # pragma: no cover - only when channels is misconfigured
        return
    message = {"type": "sevps.event", "event": event_type, "data": payload}
    try:
        async_to_sync(layer.group_send)(group, message)
    except Exception:  # pragma: no cover - fan-out must never break dispatch
        log.warning("realtime fan-out failed for group=%s event=%s", group, event_type, exc_info=True)


def broadcast_many(groups: Iterable[str], event_type: str, payload: dict[str, Any]) -> None:
    for group in dict.fromkeys(groups):  # de-duplicate, preserve order
        broadcast(group, event_type, payload)


def broadcast_ops(event_type: str, payload: dict[str, Any]) -> None:
    """Emergency Operations Dashboard (feature 4.11) sees everything."""
    broadcast(GROUP_OPS, event_type, payload)
