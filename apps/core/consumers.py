"""Base WebSocket consumer used by every SEVPS real-time endpoint."""
from __future__ import annotations

import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer


class GroupConsumer(AsyncWebsocketConsumer):
    """Subscribes a socket to one or more channel groups and streams events.

    Subclasses implement :meth:`groups_for_scope` and, optionally,
    :meth:`initial_payload` to push a snapshot on connect so a freshly opened
    dashboard is populated before the first live event arrives.
    """

    async def groups_for_scope(self) -> list[str]:
        raise NotImplementedError

    async def initial_payload(self) -> dict | None:
        return None

    async def handle_client_message(self, message: dict) -> None:
        """Override to accept client->server messages (default: ignore)."""
        return None

    async def connect(self):
        # Initialise first: a refused connection still receives a disconnect,
        # and it must not fail while tearing itself down.
        self._groups: list[str] = []

        groups = await self.groups_for_scope()
        if groups is None:
            await self.close(code=4403)
            return

        self._groups = list(groups)
        for group in self._groups:
            await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()

        snapshot = await self.initial_payload()
        if snapshot is not None:
            # Every snapshot states who the server authenticated. A client
            # otherwise has no way to tell an anonymous socket from an
            # authenticated one, which is precisely how a broken JWT handshake
            # goes unnoticed - the data just quietly arrives redacted.
            snapshot = {**snapshot, "viewer": await self.viewer_identity()}
            await self.send_event("snapshot", snapshot)

    @database_sync_to_async
    def viewer_identity(self) -> dict:
        from apps.core.roles import may_view_clinical_data, user_roles

        user = self.scope.get("user")
        if user is None or not user.is_authenticated:
            return {"authenticated": False, "username": None, "roles": []}
        return {
            "authenticated": True,
            "username": user.get_username(),
            "roles": sorted(user_roles(user)),
            "clinical_access": may_view_clinical_data(user),
            "auth_method": self.scope.get("auth_method", "session"),
        }

    async def disconnect(self, code):
        for group in getattr(self, "_groups", None) or []:
            await self.channel_layer.group_discard(group, self.channel_name)
        self._groups = []

    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_event("error", {"detail": "malformed JSON"})
            return
        if message.get("type") == "ping":
            await self.send_event("pong", {})
            return
        await self.handle_client_message(message)

    async def sevps_event(self, message):
        """Channel-layer handler - `type: "sevps.event"` maps here."""
        await self.send_event(message["event"], message["data"])

    async def send_event(self, event: str, data: dict):
        await self.send(text_data=json.dumps({"event": event, "data": data}, default=str))

    async def subscribe(self, group: str):
        if group not in self._groups:
            await self.channel_layer.group_add(group, self.channel_name)
            self._groups.append(group)

    async def unsubscribe_all(self):
        for group in list(getattr(self, "_groups", [])):
            await self.channel_layer.group_discard(group, self.channel_name)
        self._groups = []
