"""Base WebSocket consumer used by every SEVPS real-time endpoint.

Beyond group subscription this provides the things a long-lived operational
socket actually needs:

* **Authorisation.** Every consumer declares a :class:`ConsumerPolicy` and the
  base class enforces it before accepting. Authentication alone was not
  enough - Phase 2 authenticated sockets, and a probe still found patient data
  on an anonymous connection because no consumer checked a role.
* **Sequencing.** Every frame carries a monotonic ``seq``, so a client can
  detect a gap instead of silently rendering stale state.
* **Coalescing.** High-frequency events (vehicle positions) are collapsed to
  the latest value per key over a short window. Nine vehicles at 2 s is
  nothing; a city fleet at 1 s across fifty dashboards is not.
* **Selective subscription.** A client can narrow what it receives rather than
  taking the whole firehose.
"""
from __future__ import annotations

import json
import time
from typing import Any

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from apps.core.ws_policy import ConsumerPolicy

#: Close codes. 4401/4403 mirror HTTP semantics so a client can tell "sign in"
#: from "your role is not permitted" without guessing.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404

#: Events collapsed to the newest value per key inside COALESCE_WINDOW_S.
COALESCED_EVENTS = {"vehicle_position": "callsign"}
COALESCE_WINDOW_S = 0.9


class GroupConsumer(AsyncWebsocketConsumer):
    """Subscribes a socket to one or more channel groups and streams events.

    Subclasses set :attr:`policy`, implement :meth:`groups_for_scope`, and
    optionally :meth:`initial_payload` to push a snapshot on connect so a
    freshly opened dashboard is populated before the first live event.
    """

    #: Every consumer must declare one; the policy audit fails the build if not.
    policy: ConsumerPolicy | None = None

    async def groups_for_scope(self) -> list[str] | None:
        raise NotImplementedError

    async def initial_payload(self) -> dict | None:
        return None

    async def handle_client_message(self, message: dict) -> None:
        """Override to accept client->server messages (default: ignore)."""
        return None

    # ------------------------------------------------------------- lifecycle
    async def connect(self):
        # Initialise first: a refused connection still receives a disconnect,
        # and it must not fail while tearing itself down.
        self._groups: list[str] = []
        self._seq = 0
        self._filters: dict[str, set[str]] = {}
        self._pending: dict[tuple[str, str], float] = {}

        if not await self._authorise():
            return

        groups = await self.groups_for_scope()
        if groups is None:
            await self.close(code=CLOSE_NOT_FOUND)
            return

        self._groups = list(groups)
        for group in self._groups:
            await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()

        snapshot = await self.initial_payload()
        if snapshot is not None:
            # Every snapshot states who the server authenticated. A client
            # otherwise cannot tell an anonymous socket from an authenticated
            # one, which is how a broken JWT handshake goes unnoticed - the
            # data just quietly arrives redacted.
            snapshot = {**snapshot, "viewer": await self.viewer_identity()}
            await self.send_event("snapshot", snapshot)

    async def _authorise(self) -> bool:
        """Enforce the declared policy. Closes and returns False on refusal."""
        policy = self.policy
        if policy is None:
            # Fail closed: a consumer that forgot its policy must not serve.
            await self.close(code=CLOSE_FORBIDDEN)
            return False

        user = self.scope.get("user")
        if not (user and user.is_authenticated):
            if not policy.allow_anonymous:
                await self.close(code=CLOSE_UNAUTHENTICATED)
                return False
            return True

        # Role resolution reads the user's groups, so it must not run on the
        # event loop. `permits` looks like a pure predicate but is not.
        if not await self._permits(user):
            await self.close(code=CLOSE_FORBIDDEN)
            return False
        return True

    @database_sync_to_async
    def _permits(self, user) -> bool:
        return self.policy.permits(user)

    async def disconnect(self, code):
        for group in getattr(self, "_groups", None) or []:
            await self.channel_layer.group_discard(group, self.channel_name)
        self._groups = []

    # -------------------------------------------------------------- inbound
    async def receive(self, text_data=None, bytes_data=None):
        if not text_data:
            return
        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            await self.send_event("error", {"detail": "malformed JSON"})
            return

        kind = message.get("type")
        if kind == "ping":
            await self.send_event("pong", {"server_time": time.time()})
            return
        if kind == "subscribe":
            await self._apply_filters(message)
            return

        # A socket that does not accept commands ignores everything else -
        # stated rather than silently dropped, so a client is not left
        # wondering why its message had no effect.
        if not (self.policy and self.policy.accepts_commands):
            await self.send_event(
                "error", {"detail": "this socket is read-only", "type": kind}
            )
            return

        await self.handle_client_message(message)

    async def _apply_filters(self, message: dict) -> None:
        """Narrow what this socket receives.

        ``{"type": "subscribe", "filters": {"event": ["vehicle_position"],
        "callsign": ["AMB-101"]}}``

        A dashboard following one vehicle should not pay for the whole city.
        Filtering here rather than at the publisher keeps fan-out simple and
        means one client's preferences cannot affect another's.
        """
        raw = message.get("filters")
        if not isinstance(raw, dict):
            self._filters = {}
            await self.send_event("subscribed", {"filters": {}})
            return

        filters: dict[str, set[str]] = {}
        for key, values in raw.items():
            if isinstance(values, (list, tuple, set)) and values:
                filters[str(key)] = {str(v) for v in values}
        self._filters = filters
        await self.send_event(
            "subscribed", {"filters": {k: sorted(v) for k, v in filters.items()}}
        )

    # ------------------------------------------------------------- outbound
    async def sevps_event(self, message):
        """Channel-layer handler - ``type: "sevps.event"`` maps here."""
        event = message["event"]
        data = message["data"]

        if not self._passes_filters(event, data):
            return
        if self._should_coalesce(event, data):
            return
        await self.send_event(event, data)

    def _passes_filters(self, event: str, data: Any) -> bool:
        if not self._filters:
            return True
        if (wanted := self._filters.get("event")) and event not in wanted:
            return False
        if isinstance(data, dict):
            for key, wanted in self._filters.items():
                if key == "event":
                    continue
                value = data.get(key)
                # A filter only excludes when the field is present and differs;
                # an unrelated event without that key still gets through.
                if value is not None and str(value) not in wanted:
                    return False
        return True

    def _should_coalesce(self, event: str, data: Any) -> bool:
        """Drop a high-frequency update superseded within the window."""
        key_field = COALESCED_EVENTS.get(event)
        if key_field is None or not isinstance(data, dict):
            return False

        key = (event, str(data.get(key_field, "")))
        now = time.monotonic()
        last = self._pending.get(key, 0.0)
        if now - last < COALESCE_WINDOW_S:
            return True
        self._pending[key] = now
        return False

    async def send_event(self, event: str, data: dict):
        self._seq += 1
        await self.send(
            text_data=json.dumps(
                {"event": event, "seq": self._seq, "ts": time.time(), "data": data},
                default=str,
            )
        )

    # -------------------------------------------------------------- helpers
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

    async def subscribe(self, group: str):
        if group not in self._groups:
            await self.channel_layer.group_add(group, self.channel_name)
            self._groups.append(group)

    async def unsubscribe_all(self):
        for group in list(getattr(self, "_groups", [])):
            await self.channel_layer.group_discard(group, self.channel_name)
        self._groups = []
