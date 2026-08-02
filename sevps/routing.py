"""WebSocket URL routing for the SEVPS real-time layer.

Channel groups
--------------
``ops``                 - Emergency Operations Dashboard (4.11): every event.
``hospital.<code>``     - Hospital Preparedness Dashboard (4.7): inbound trips.
``vehicle.<callsign>``  - Onboard/paramedic app: route + siren directives.
``drivers.<geohash>``   - Layer 4 driver alerts, sharded by ~1.2 km geohash cell.
``signals``             - Traffic signal controller bridge (Layer 3).
"""
from django.urls import path

from apps.alerts.consumers import DriverAlertConsumer
from apps.dashboards.consumers import OpsConsumer
from apps.dispatch.consumers import SignalControlConsumer, VehicleConsumer
from apps.hospitals.consumers import HospitalConsumer

websocket_urlpatterns = [
    path("ws/ops/", OpsConsumer.as_asgi()),
    path("ws/hospital/<str:code>/", HospitalConsumer.as_asgi()),
    path("ws/vehicle/<str:callsign>/", VehicleConsumer.as_asgi()),
    path("ws/drivers/", DriverAlertConsumer.as_asgi()),
    path("ws/signals/", SignalControlConsumer.as_asgi()),
]
