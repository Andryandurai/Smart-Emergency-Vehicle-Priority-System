"""Emergency Operations Dashboard socket (feature 4.11).

Subscribes to the firehose ``ops`` group and pushes a full snapshot on
connect, so a control room that opens the page mid-incident sees the current
state immediately rather than waiting for the next event.
"""
from channels.db import database_sync_to_async

from apps.core.consumers import GroupConsumer
from apps.core.realtime import GROUP_OPS


class OpsConsumer(GroupConsumer):
    async def groups_for_scope(self):
        return [GROUP_OPS]

    async def initial_payload(self):
        # The scope user comes from JWTAuthMiddlewareStack (JWT or session).
        # Passing it through means clinical fields are redacted for roles
        # without clearance on the socket exactly as they are over REST.
        return await self._snapshot(self.scope.get("user"))

    @database_sync_to_async
    def _snapshot(self, user) -> dict:
        from apps.alerts.models import DisplayBoard, DriverAlert
        from apps.dispatch.models import EmergencyTrip, SignalPreemption
        from apps.dispatch.serializers import EmergencyTripSerializer, SignalPreemptionSerializer
        from apps.fleet.models import EmergencyVehicle
        from apps.hospitals.models import Hospital
        from apps.hospitals.serializers import HospitalSummarySerializer
        from apps.network.models import RoadEvent, TrafficSignal
        from apps.network.serializers import RoadEventSerializer, TrafficSignalSerializer
        from django.utils import timezone

        trips = (
            EmergencyTrip.objects.active()
            .select_related("vehicle", "destination_hospital")
            .prefetch_related("routes")
        )
        preemptions = SignalPreemption.objects.filter(
            state__in=["planned", "armed", "active"]
        ).select_related("signal", "signal__intersection", "trip", "trip__vehicle")

        return {
            "vehicles": [
                v.as_tracking_payload() for v in EmergencyVehicle.objects.online()
            ],
            "trips": EmergencyTripSerializer(
                trips, many=True, context={"user": user}
            ).data,
            "routes": [
                {"trip_id": t.id, "geometry": r.geometry, "eta": r.predicted_eta}
                for t in trips
                if (r := t.active_route) is not None
            ],
            "preemptions": SignalPreemptionSerializer(preemptions, many=True).data,
            "signals": TrafficSignalSerializer(
                TrafficSignal.objects.select_related("intersection")[:500], many=True
            ).data,
            "events": RoadEventSerializer(
                RoadEvent.objects.active().select_related("segment"), many=True
            ).data,
            "hospitals": HospitalSummarySerializer(
                Hospital.objects.filter(is_active=True), many=True
            ).data,
            "boards": [
                {
                    "code": b.code,
                    "latitude": b.latitude,
                    "longitude": b.longitude,
                    "message": b.current_message if b.is_displaying_alert else "",
                }
                for b in DisplayBoard.objects.filter(is_active=True)
            ],
            "recent_alerts": [
                a.as_payload()
                for a in DriverAlert.objects.filter(expires_at__gt=timezone.now())[:50]
            ],
        }
