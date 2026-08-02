"""Hospital Preparedness Dashboard socket (feature 4.7).

A hospital subscribes to its own group and receives, live: inbound ambulance
position, ETA, route status, emergency type and distance remaining - exactly
the payload set the problem statement specifies.

Security note, because this consumer had a real defect: the snapshot used to
build its inbound list from ``EmergencyTrip.as_hospital_payload()``, a model
method that predates the Phase 2 clinical redaction and therefore bypassed it
entirely. Combined with the absence of any role check, an anonymous socket
could read patient age and clinical notes. Both halves are fixed here: the
policy restricts who may connect, and the payload goes through the serializer
so redaction applies to whoever does.
"""
from channels.db import database_sync_to_async

from apps.core.consumers import GroupConsumer
from apps.core.realtime import hospital_group
from apps.core.ws_policy import HOSPITAL_POLICY


class HospitalConsumer(GroupConsumer):
    policy = HOSPITAL_POLICY

    async def groups_for_scope(self):
        code = self.scope["url_route"]["kwargs"]["code"]
        if not await self._hospital_exists(code):
            return None
        self.hospital_code = code
        return [hospital_group(code)]

    async def initial_payload(self):
        return await self._snapshot(self.hospital_code, self.scope.get("user"))

    @database_sync_to_async
    def _hospital_exists(self, code: str) -> bool:
        from apps.hospitals.models import Hospital

        return Hospital.objects.filter(code__iexact=code, is_active=True).exists()

    @database_sync_to_async
    def _snapshot(self, code: str, user) -> dict:
        from apps.dispatch.models import EmergencyTrip
        from apps.dispatch.serializers import EmergencyTripSerializer
        from apps.hospitals.models import Hospital, HospitalAlert
        from apps.hospitals.serializers import (
            HospitalAlertSerializer,
            HospitalCapacitySerializer,
            HospitalSerializer,
        )

        hospital = Hospital.objects.prefetch_related("capabilities").get(code__iexact=code)
        trips = (
            EmergencyTrip.objects.active()
            .filter(destination_hospital=hospital)
            .select_related("vehicle", "destination_hospital")
        )
        alerts = HospitalAlert.objects.filter(hospital=hospital).order_by("-created_at")[:20]

        return {
            "hospital": HospitalSerializer(hospital).data,
            "capacity": HospitalCapacitySerializer(hospital.capacity).data,
            # Through the serializer, with the socket's user in context, so the
            # same redaction rules apply here as over REST.
            "inbound": EmergencyTripSerializer(
                trips, many=True, context={"user": user}
            ).data,
            "alerts": HospitalAlertSerializer(alerts, many=True).data,
        }
