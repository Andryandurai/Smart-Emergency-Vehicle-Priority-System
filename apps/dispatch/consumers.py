"""Dispatch WebSockets: the onboard/paramedic app and the signal bridge."""
from __future__ import annotations

from channels.db import database_sync_to_async

from apps.core.consumers import GroupConsumer
from apps.core.realtime import GROUP_SIGNALS, vehicle_group


class VehicleConsumer(GroupConsumer):
    """Onboard unit socket.

    Receives route updates and Layer 6 light/siren directives; accepts
    telemetry pushed up the same socket so a vehicle in poor coverage does
    not pay HTTP handshake costs on every fix.
    """

    async def groups_for_scope(self):
        callsign = self.scope["url_route"]["kwargs"]["callsign"]
        if not await self._vehicle_exists(callsign):
            return None
        self.callsign = callsign
        return [vehicle_group(callsign)]

    async def initial_payload(self):
        return await self._snapshot(self.callsign, self.scope.get("user"))

    async def handle_client_message(self, message: dict):
        if message.get("type") != "telemetry":
            return
        try:
            result = await self._ingest(self.callsign, message)
        except (KeyError, TypeError, ValueError) as exc:
            await self.send_event("error", {"detail": f"invalid telemetry: {exc}"})
            return
        await self.send_event("telemetry_ack", result)

    @database_sync_to_async
    def _vehicle_exists(self, callsign: str) -> bool:
        from apps.fleet.models import EmergencyVehicle

        return EmergencyVehicle.objects.filter(callsign__iexact=callsign).exists()

    @database_sync_to_async
    def _snapshot(self, callsign: str, user) -> dict:
        from apps.dispatch.corridor import corridor_status
        from apps.dispatch.serializers import EmergencyTripSerializer
        from apps.dispatch.siren import profile_for
        from apps.fleet.models import EmergencyVehicle

        vehicle = EmergencyVehicle.objects.get(callsign__iexact=callsign)
        trip = vehicle.active_trip
        plan = trip.active_route if trip else None
        return {
            "vehicle": vehicle.as_tracking_payload(),
            "priority_profile": profile_for(vehicle.priority_level).as_dict(),
            "trip": (
                EmergencyTripSerializer(trip, context={"user": user}).data
                if trip
                else None
            ),
            "route": {"geometry": plan.geometry, "eta": plan.predicted_eta} if plan else None,
            "corridor": corridor_status(trip) if trip else [],
        }

    @database_sync_to_async
    def _ingest(self, callsign: str, message: dict) -> dict:
        from apps.dispatch.orchestrator import on_vehicle_position
        from apps.fleet.models import EmergencyVehicle

        vehicle = EmergencyVehicle.objects.get(callsign__iexact=callsign)
        vehicle.record_position(
            float(message["latitude"]),
            float(message["longitude"]),
            speed_kmh=message.get("speed_kmh"),
            heading_deg=message.get("heading_deg"),
            accuracy_m=message.get("accuracy_m"),
        )
        outcome = on_vehicle_position(vehicle)
        return {"accepted": True, "trip": outcome}


class SignalControlConsumer(GroupConsumer):
    """Bridge socket for physical signal controllers.

    A cabinet-side agent connects here, receives ``signal_command`` events for
    its controllers and reports phase changes back.  This is how SEVPS reaches
    hardware that cannot expose an inbound HTTP endpoint.
    """

    async def groups_for_scope(self):
        return [GROUP_SIGNALS]

    async def initial_payload(self):
        return await self._active_preemptions()

    async def handle_client_message(self, message: dict):
        if message.get("type") == "phase_report":
            await self._record_phase(message)
            await self.send_event("phase_ack", {"controller_id": message.get("controller_id")})

    @database_sync_to_async
    def _active_preemptions(self) -> dict:
        from apps.dispatch.models import SignalPreemption
        from apps.dispatch.serializers import SignalPreemptionSerializer

        rows = SignalPreemption.objects.filter(
            state__in=["planned", "armed", "active"]
        ).select_related("signal", "signal__intersection", "trip")
        return {"active_preemptions": SignalPreemptionSerializer(rows, many=True).data}

    @database_sync_to_async
    def _record_phase(self, message: dict) -> None:
        from django.utils import timezone

        from apps.network.models import TrafficSignal

        signal = TrafficSignal.objects.filter(
            controller_id=message.get("controller_id")
        ).first()
        if signal is None:
            return
        signal.current_phase = message.get("phase", signal.current_phase)
        signal.last_heartbeat = timezone.now()
        signal.is_online = True
        signal.save(
            update_fields=["current_phase", "last_heartbeat", "is_online", "updated_at"]
        )
