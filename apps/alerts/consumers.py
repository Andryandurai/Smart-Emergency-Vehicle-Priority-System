"""Driver alert WebSocket (Layer 4).

A road user's app connects once and then reports its position; the consumer
re-subscribes it to the geohash cells it is currently in.  Subscribing to the
containing cell *and its eight neighbours* removes the boundary case where a
driver sitting a few metres inside the next cell misses the warning.
"""
from __future__ import annotations

from channels.db import database_sync_to_async

from apps.core.consumers import GroupConsumer
from apps.core.geo import geohash_neighbours
from apps.core.ws_policy import DRIVERS_POLICY


class DriverAlertConsumer(GroupConsumer):
    policy = DRIVERS_POLICY

    async def groups_for_scope(self):
        self.cells: set[str] = set()
        self.device_id: str | None = None
        query = self.scope.get("query_string", b"").decode()
        params = dict(
            pair.split("=", 1) for pair in query.split("&") if "=" in pair
        )
        try:
            lat = float(params["lat"])
            lon = float(params["lon"])
        except (KeyError, ValueError):
            # Connect without a position; the client sends one shortly after.
            return []
        self.device_id = params.get("device_id")
        return await self._cells_for(lat, lon)

    async def initial_payload(self):
        return {"subscribed_cells": sorted(self.cells)}

    async def handle_client_message(self, message: dict):
        if message.get("type") != "position":
            return
        try:
            lat = float(message["latitude"])
            lon = float(message["longitude"])
        except (KeyError, TypeError, ValueError):
            await self.send_event("error", {"detail": "latitude/longitude required"})
            return

        self.device_id = message.get("device_id", self.device_id)
        wanted = set(await self._cells_for(lat, lon, register=True, message=message))

        for stale in self.cells - wanted:
            await self.channel_layer.group_discard(f"drivers.{stale}", self.channel_name)
        for fresh in wanted - self.cells:
            await self.channel_layer.group_add(f"drivers.{fresh}", self.channel_name)

        self.cells = wanted
        self._groups = [f"drivers.{c}" for c in wanted]
        await self.send_event("subscribed", {"cells": sorted(wanted)})

        for alert in await self._live_alerts(lat, lon):
            await self.send_event("driver_alert", alert)

    async def _cells_for(self, lat: float, lon: float, *, register=False, message=None):
        cells = set(geohash_neighbours(lat, lon, 6))
        self.cells = cells
        if register and self.device_id:
            await self._register_device(lat, lon, message or {})
        return [f"drivers.{c}" for c in cells]

    @database_sync_to_async
    def _register_device(self, lat: float, lon: float, message: dict):
        from apps.alerts.models import DriverDevice

        device, _ = DriverDevice.objects.get_or_create(
            device_id=self.device_id,
            defaults={"latitude": lat, "longitude": lon},
        )
        device.update_position(
            lat,
            lon,
            heading_deg=message.get("heading_deg", device.heading_deg),
            speed_kmh=message.get("speed_kmh", device.speed_kmh),
        )

    @database_sync_to_async
    def _live_alerts(self, lat: float, lon: float):
        from apps.alerts.dispatcher import active_alerts_near

        return active_alerts_near(lat, lon)
