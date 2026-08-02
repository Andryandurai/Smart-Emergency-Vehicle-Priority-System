"""Add PostGIS geography columns and GiST indexes.

Vendor-aware by design: on SQLite every operation is a no-op, so the same
migration graph applies to a laptop pilot and a city deployment. That matters
more than it sounds - a migration history that diverges by backend is a
migration history nobody can safely roll back.

The point columns are ``GENERATED ALWAYS ... STORED`` from the existing
``latitude``/``longitude`` floats, so they cannot drift from the source of
truth and need no application code to maintain. The LineString columns cannot
be generated (the JSON is ``[lat, lon]`` while PostGIS wants ``(lon, lat)``,
which needs an array walk) and are filled by ``manage.py backfill_geometry``.
"""
from django.db import migrations

from apps.core.spatial import (
    LINESTRING_TABLES,
    POINT_TABLES,
    drop_generated_column_sql,
    drop_linestring_column_sql,
    generated_column_sql,
    linestring_column_sql,
)


def _table_for(apps_registry, app_label: str, model_name: str) -> str:
    return apps_registry.get_model(app_label, model_name)._meta.db_table


def add_spatial_columns(apps_registry, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS postgis")

        for app_label, model_name in POINT_TABLES:
            table = _table_for(apps_registry, app_label, model_name)
            cursor.execute(generated_column_sql(table))

        for app_label, model_name, _json_field in LINESTRING_TABLES:
            table = _table_for(apps_registry, app_label, model_name)
            cursor.execute(linestring_column_sql(table))


def drop_spatial_columns(apps_registry, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return

    with schema_editor.connection.cursor() as cursor:
        for app_label, model_name in POINT_TABLES:
            cursor.execute(
                drop_generated_column_sql(_table_for(apps_registry, app_label, model_name))
            )
        for app_label, model_name, _json_field in LINESTRING_TABLES:
            cursor.execute(
                drop_linestring_column_sql(_table_for(apps_registry, app_label, model_name))
            )
        # The extension is deliberately left installed: other schemas may use
        # it, and dropping it is not this migration's decision to make.


class Migration(migrations.Migration):
    """Runs after every table that gains a spatial column exists."""

    initial = True

    dependencies = [
        ("fleet", "0001_initial"),
        ("network", "0001_initial"),
        ("hospitals", "0001_initial"),
        ("dispatch", "0002_initial"),
        ("alerts", "0002_initial"),
        ("analytics", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            add_spatial_columns,
            reverse_code=drop_spatial_columns,
            # No model state changes: the columns are database-side only, so
            # Django's autodetector must not try to reconcile them.
            elidable=False,
        ),
    ]
