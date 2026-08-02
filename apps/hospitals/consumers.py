"""Hospital Preparedness Dashboard socket (feature 4.7).

A hospital subscribes to its own group and receives, live: inbound ambulance
position, ETA, route status, emergency type and distance remaining - exactly
the payload set the problem statement specifies.
"""
from channels.db import database_sync_to_async

from apps.core.consumers import GroupConsumer
from apps.core.realtime import hospital_group


class HospitalConsumer(GroupConsumer):
    async def groups_for_scope(self):
        code = self.scope["url_route"]["kwargs"]["code"]
        if not await self._hospital_exists(code):
            return None
        self.hospital_code = code
        return [hospital_group(code)]

    async def initial_payload(self):
        return await self._snapshot(self.hospital_code)

    @database_sync_to_async
    def _hospital_exists(self, code: str) -> bool:
        from apps.hospitals.models import Hospital

        return Hospital.objects.filter(code__iexact=code, is_active=True).exists()

    @database_sync_to_async
    def _snapshot(self, code: str) -> dict:
        from apps.dispatch.models import EmergencyTrip
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
            .select_related("vehicle")
        )
        alerts = HospitalAlert.objects.filter(hospital=hospital).order_by("-created_at")[:20]

        return {
            "hospital": HospitalSerializer(hospital).data,
            "capacity": HospitalCapacitySerializer(hospital.capacity).data,
            "inbound": [t.as_hospital_payload() for t in trips],
            "alerts": HospitalAlertSerializer(alerts, many=True).data,
        }
