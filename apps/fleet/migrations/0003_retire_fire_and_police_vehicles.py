"""Retire the fire-engine and police vehicle types.

SEVPS dispatches patient transport: every downstream stage - emergency
category, hospital capability matching, bed capacity, clinical handover -
assumes a patient. Keeping fire and police on the fleet list invited an
operator to dispatch one down a route planned against hospital beds.

Existing rows are converted to ``disaster`` (Disaster Response Unit) rather
than deleted, because a vehicle has telemetry history, completed trips and
analytics rows hanging off it, and destroying an audit trail to tidy a
dropdown is the wrong trade. Nothing is dispatched to them - they are not
ambulances, so hospital recommendation never selects them.

``Role.TRAFFIC_POLICE`` is untouched; that is the signal control room, not a
vehicle.
"""
from django.db import migrations, models


RETIRED = ("fire_engine", "police")


def convert_retired_types(apps, schema_editor):
    EmergencyVehicle = apps.get_model("fleet", "EmergencyVehicle")
    # ``offline``, not ``out_of_service``: VehicleQuerySet.online() only
    # excludes OFFLINE, so an out-of-service vehicle would still be drawn on
    # the operations map - which is exactly what retiring the type was meant
    # to stop.
    EmergencyVehicle.objects.filter(vehicle_type__in=RETIRED).update(
        vehicle_type="disaster",
        status="offline",
    )


def restore_is_not_possible(apps, schema_editor):
    """One-way.

    Which of the two retired types a converted row used to be is not recorded
    anywhere, so reversing would have to guess. Left as a no-op so the
    migration can still be unapplied without erroring.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("fleet", "0002_emergencyvehicle_ownership"),
    ]

    operations = [
        migrations.RunPython(convert_retired_types, restore_is_not_possible),
        migrations.AlterField(
            model_name="emergencyvehicle",
            name="vehicle_type",
            field=models.CharField(
                choices=[
                    ("ambulance", "Ambulance"),
                    ("disaster", "Disaster Response Unit"),
                ],
                db_index=True,
                default="ambulance",
                max_length=20,
            ),
        ),
    ]
