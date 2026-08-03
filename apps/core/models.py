"""Abstract base models and geo-aware querysets shared by all SEVPS apps."""
from __future__ import annotations

import uuid

from django.db import models

from apps.core.geo import Point, bounding_box, haversine_m


class GeoQuerySet(models.QuerySet):
    """Adds radius search to any model carrying ``latitude``/``longitude``."""

    def in_bbox(self, lat: float, lon: float, radius_m: float):
        """Index-friendly coarse filter - a strict superset of the true circle."""
        min_lat, min_lon, max_lat, max_lon = bounding_box(lat, lon, radius_m)
        return self.filter(
            latitude__gte=min_lat,
            latitude__lte=max_lat,
            longitude__gte=min_lon,
            longitude__lte=max_lon,
        )

    def near(self, lat: float, lon: float, radius_m: float) -> list:
        """Exact radius search, nearest first.

        Returns a **list**, not a queryset, with ``distance_m`` set on every
        object. Six call sites depend on that contract, so it is preserved
        exactly across both spatial backends.

        On PostGIS this becomes an indexed ``ST_DWithin`` with KNN ordering;
        on SQLite it stays a bounding-box pre-filter plus an exact haversine
        test. Same results either way - asserted in ``tests_spatial.py``.
        """
        from apps.core.spatial import near_queryset

        return near_queryset(self, lat, lon, radius_m)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UUIDModel(models.Model):
    """Public identifier that is safe to expose to mobile clients."""

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)

    class Meta:
        abstract = True


class GeoPointModel(models.Model):
    """A single WGS84 position.

    Stored as two indexed floats rather than a PostGIS geometry so the schema
    is portable; when PostGIS is enabled the same columns are wrapped by a
    generated geography index (see docs/DEPLOYMENT.md).
    """

    latitude = models.FloatField(db_index=True)
    longitude = models.FloatField(db_index=True)

    objects = GeoQuerySet.as_manager()

    class Meta:
        abstract = True

    @property
    def point(self) -> Point:
        return Point(self.latitude, self.longitude)

    def distance_to(self, other) -> float:
        """Metres to another GeoPointModel or :class:`Point`."""
        lat = getattr(other, "latitude", None)
        lon = getattr(other, "longitude", None)
        if lat is None:
            lat, lon = other.lat, other.lon
        return haversine_m(self.latitude, self.longitude, lat, lon)

    def as_geojson_feature(self, properties: dict | None = None) -> dict:
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.longitude, self.latitude]},
            "properties": properties or {},
        }


# ---------------------------------------------------------------------------
# Staff identity. Re-exported so ``from apps.core.models import StaffProfile``
# works like every other model in the project.
# ---------------------------------------------------------------------------
from apps.core.profiles import StaffProfile  # noqa: E402,F401
