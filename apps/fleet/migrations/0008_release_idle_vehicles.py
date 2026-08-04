"""Return crewless, jobless ambulances to the available pool.

Ending a shift closed the shift and left the vehicle wherever the last job
had put it - ``at_hospital``, ``returning``, ``on_scene``. Nothing ever moved
it back, so an ambulance that finished a run became crewless *and*
permanently invisible to the takeover picker, which offers available vehicles
only. On the deployment this was found on, six of ten ambulances had been
retired this way and a driver signing on saw a near-empty board.

``CrewShift.release_vehicle`` now does this at the moment a shift ends. This
migration repairs the vehicles stranded before it existed.

Deliberately narrow. Only the five in-job statuses are touched: ``offline``
means the onboard unit is not reporting and ``out_of_service`` is somebody's
explicit decision, and neither is something a finished shift may overrule.
Readiness is not touched at all - a grounded ambulance stays grounded.
"""
from django.db import migrations
from django.utils import timezone

HELD = ["dispatched", "on_scene", "transporting", "at_hospital", "returning"]
OPEN_SHIFT = ["draft", "pending", "active"]
CLOSED_TRIP = ["arrived", "handover", "cancelled"]


def release_idle_vehicles(apps, schema_editor):
    EmergencyVehicle = apps.get_model("fleet", "EmergencyVehicle")
    CrewShift = apps.get_model("fleet", "CrewShift")
    EmergencyTrip = apps.get_model("dispatch", "EmergencyTrip")

    crewed = set(
        CrewShift.objects.filter(status__in=OPEN_SHIFT).values_list("vehicle_id", flat=True)
    )
    # Still carrying a patient: the vehicle is genuinely busy whatever its
    # crew did, and must not be offered to the next driver.
    on_a_job = set(
        EmergencyTrip.objects.exclude(stage__in=CLOSED_TRIP).values_list(
            "vehicle_id", flat=True
        )
    )

    (
        EmergencyVehicle.objects.filter(status__in=HELD)
        .exclude(id__in=crewed | on_a_job)
        .update(status="available", updated_at=timezone.now())
    )


def noop(apps, schema_editor):
    """Irreversible by design - the previous status is not recoverable."""


class Migration(migrations.Migration):

    dependencies = [
        ("fleet", "0007_vehicle_is_demo"),
        ("dispatch", "0004_trip_admitted_at"),
    ]

    operations = [
        migrations.RunPython(release_idle_vehicles, noop),
    ]
