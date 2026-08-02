"""Spatial backend abstraction: PostGIS when available, haversine otherwise.

Design decision, and the reasoning matters because it shapes the whole phase:

**Latitude/longitude floats stay the canonical storage.** They are portable,
already correct, and every one of the 13 geo-bearing models uses them. On
PostgreSQL a *generated* ``geom`` column is derived from them:

    ALTER TABLE ... ADD COLUMN geom geography(Point, 4326)
      GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)::geography)
      STORED;
    CREATE INDEX ... USING GIST (geom);

The alternative - adding a GeoDjango ``PointField`` mirrored from the floats -
needs GDAL on every machine, needs sync code or a trigger on every write, and
introduces a class of bug where the point and the floats disagree. A generated
column *cannot* drift: PostgreSQL recomputes it from the same source of truth
on every write, and it is indexable exactly like a stored geometry.

So PostGIS is genuinely adopted - real ``geography`` type, real GiST index,
real ``ST_DWithin`` and KNN ordering - without GDAL becoming a hard dependency
of the application or SQLite losing a feature.

Both backends must return identical results; ``apps/core/tests_spatial.py``
asserts that on the same fixtures.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from django.conf import settings
from django.db import connection

from apps.core.geo import bounding_box, haversine_m

log = logging.getLogger("sevps.spatial")

#: Metres per degree of latitude - used only for the SQLite bbox pre-filter.
EARTH_RADIUS_M = 6_371_008.8


@lru_cache(maxsize=1)
def postgis_available() -> bool:
    """Is the connected database actually PostGIS-enabled?

    Checked against the live connection rather than a settings flag, because
    ``SEVPS_ENABLE_POSTGIS=1`` against a database where the extension was
    never created is a configuration error that should degrade rather than
    500 on the first radius search.
    """
    if connection.vendor != "postgresql":
        return False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'postgis'")
            return cursor.fetchone() is not None
    except Exception:  # pragma: no cover - unreachable database
        log.warning("could not probe for PostGIS", exc_info=True)
        return False


def reset_backend_cache() -> None:
    """Forget the probe result - used by tests and after running migrations."""
    postgis_available.cache_clear()


def backend_name() -> str:
    return "postgis" if postgis_available() else f"{connection.vendor}+haversine"


def _table_and_pk(model) -> tuple[str, str]:
    return model._meta.db_table, model._meta.pk.column


def near_queryset(queryset, lat: float, lon: float, radius_m: float):
    """Radius search returning objects annotated with ``distance_m``, nearest first.

    Contract is identical on both backends and matches the pre-existing
    ``GeoQuerySet.near()``: a **list**, not a queryset, with ``distance_m``
    set on every object. Six call sites depend on that, so the PostGIS path
    deliberately does not "improve" it into a lazy queryset.
    """
    if postgis_available():
        return _near_postgis(queryset, lat, lon, radius_m)
    return _near_haversine(queryset, lat, lon, radius_m)


def _near_haversine(queryset, lat: float, lon: float, radius_m: float) -> list:
    """Bounding-box pre-filter (index-friendly) then an exact haversine test."""
    min_lat, min_lon, max_lat, max_lon = bounding_box(lat, lon, radius_m)
    candidates = queryset.filter(
        latitude__gte=min_lat,
        latitude__lte=max_lat,
        longitude__gte=min_lon,
        longitude__lte=max_lon,
    )

    results = []
    for obj in candidates:
        distance = haversine_m(lat, lon, obj.latitude, obj.longitude)
        if distance <= radius_m:
            obj.distance_m = distance
            results.append(obj)
    results.sort(key=lambda o: o.distance_m)
    return results


def _near_postgis(queryset, lat: float, lon: float, radius_m: float) -> list:
    """``ST_DWithin`` on the generated geography column, ordered by KNN.

    ``ST_DWithin`` on a ``geography`` uses the GiST index and computes true
    spheroidal distance, so this returns the same set as the haversine path
    (to within millimetres) while scanning an index instead of a bounding box.
    """
    table, pk = _table_and_pk(queryset.model)
    point = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography"

    # Distances come back from the database; the ORM then re-fetches the rows
    # so callers still receive real model instances with their select_related
    # intact, rather than a second, subtly different object type.
    sql = (
        f'SELECT "{pk}", ST_Distance("geom", {point}) AS distance_m '
        f'FROM "{table}" '
        f'WHERE "geom" IS NOT NULL AND ST_DWithin("geom", {point}, %s) '
        f'ORDER BY "geom" <-> {point}'
    )
    params = [lon, lat, lon, lat, radius_m, lon, lat]

    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    if not rows:
        return []

    distances = {pk_value: distance for pk_value, distance in rows}
    # Re-apply the caller's filters so permissions/scoping are not bypassed.
    objects = {obj.pk: obj for obj in queryset.filter(pk__in=distances)}

    results = []
    for pk_value, distance in rows:            # already KNN-ordered
        obj = objects.get(pk_value)
        if obj is None:                        # excluded by the caller's filter
            continue
        obj.distance_m = distance
        results.append(obj)
    return results


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------
def generated_column_sql(table: str, column: str = "geom") -> str:
    """DDL adding the generated geography column and its GiST index."""
    index = f"{table}_{column}_gist"
    return (
        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" '
        f"geography(Point, 4326) GENERATED ALWAYS AS "
        f'(ST_SetSRID(ST_MakePoint("longitude", "latitude"), 4326)::geography) STORED; '
        f'CREATE INDEX IF NOT EXISTS "{index}" ON "{table}" USING GIST ("{column}");'
    )


def drop_generated_column_sql(table: str, column: str = "geom") -> str:
    index = f"{table}_{column}_gist"
    return (
        f'DROP INDEX IF EXISTS "{index}"; '
        f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{column}";'
    )


def linestring_column_sql(table: str, column: str = "path") -> str:
    """Route/segment geometry.

    ``geometry`` json is ``[[lat, lon], ...]`` while PostGIS wants (x=lon,
    y=lat), so this cannot be a generated column - the array has to be walked.
    It is therefore a plain column maintained by
    ``manage.py backfill_geometry``, which is idempotent.
    """
    index = f"{table}_{column}_gist"
    return (
        f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" '
        f"geography(LineString, 4326); "
        f'CREATE INDEX IF NOT EXISTS "{index}" ON "{table}" USING GIST ("{column}");'
    )


def drop_linestring_column_sql(table: str, column: str = "path") -> str:
    return drop_generated_column_sql(table, column)


#: Models whose ``latitude``/``longitude`` gain a generated geography column.
POINT_TABLES: tuple[tuple[str, str], ...] = (
    ("fleet", "Station"),
    ("fleet", "EmergencyVehicle"),
    ("fleet", "VehicleTelemetry"),
    ("network", "Intersection"),
    ("network", "RoadSegment"),
    ("network", "CameraFeed"),
    ("network", "RoadEvent"),
    ("network", "AccidentRecord"),
    ("hospitals", "Hospital"),
    ("alerts", "DisplayBoard"),
    ("alerts", "DriverDevice"),
    ("alerts", "DriverAlert"),
    ("analytics", "Hotspot"),
)

#: Models carrying a polyline in a JSON column that also gets a LineString.
LINESTRING_TABLES: tuple[tuple[str, str, str], ...] = (
    ("network", "RoadSegment", "geometry"),
    ("dispatch", "RoutePlan", "geometry"),
)


def spatial_status() -> dict:
    """Reported by ``/api/v1/info/`` so the live backend is never a guess."""
    status = {
        "backend": backend_name(),
        "postgis": postgis_available(),
        "enabled_in_settings": bool(settings.SEVPS.get("POSTGIS_ENABLED")),
    }
    if postgis_available():
        with connection.cursor() as cursor:
            cursor.execute("SELECT PostGIS_Lib_Version()")
            status["postgis_version"] = cursor.fetchone()[0]
    return status
