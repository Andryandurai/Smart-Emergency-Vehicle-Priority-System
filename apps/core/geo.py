"""Geospatial primitives used across every SEVPS layer.

Everything here is pure-Python WGS84 maths so the platform runs identically on
SQLite (pilot / laptop) and PostgreSQL.  When ``SEVPS_ENABLE_POSTGIS=1`` the
same helpers remain valid - PostGIS is then used for *indexed* candidate
lookups only, and these functions do the exact scoring.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

EARTH_RADIUS_M = 6_371_008.8
_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


@dataclass(frozen=True)
class Point:
    """An immutable WGS84 coordinate."""

    lat: float
    lon: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lon)

    def as_geojson(self) -> list[float]:
        """GeoJSON uses [lon, lat] ordering."""
        return [self.lon, self.lat]


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def distance_m(a: Point, b: Point) -> float:
    return haversine_m(a.lat, a.lon, b.lat, b.lon)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2, in degrees [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def bearing_delta(a: float, b: float) -> float:
    """Smallest absolute angle between two bearings, in degrees [0, 180]."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def destination_point(lat: float, lon: float, bearing: float, distance_m_: float) -> Point:
    """Project a point `distance_m_` metres along `bearing` degrees."""
    ang = distance_m_ / EARTH_RADIUS_M
    br = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(ang) + math.cos(p1) * math.sin(ang) * math.cos(br))
    l2 = l1 + math.atan2(
        math.sin(br) * math.sin(ang) * math.cos(p1),
        math.cos(ang) - math.sin(p1) * math.sin(p2),
    )
    return Point(math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0)


def bounding_box(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Cheap lat/lon envelope for SQL pre-filtering: (min_lat, min_lon, max_lat, max_lon).

    Always a superset of the true circle, so it is safe to use as a coarse
    index filter before applying :func:`haversine_m` for the exact test.
    """
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    dlon = math.degrees(radius_m / (EARTH_RADIUS_M * cos_lat))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def interpolate(a: Point, b: Point, fraction: float) -> Point:
    """Linear interpolation between two nearby points (fine at city scale)."""
    f = min(1.0, max(0.0, fraction))
    return Point(a.lat + (b.lat - a.lat) * f, a.lon + (b.lon - a.lon) * f)


def project_on_segment(p: Point, a: Point, b: Point) -> tuple[Point, float, float]:
    """Project ``p`` onto segment a->b.

    Returns ``(closest_point, distance_from_p_m, fraction_along_segment)``.
    Uses a local equirectangular projection - accurate to well under a metre
    over the length of a city street.
    """
    lat0 = math.radians((a.lat + b.lat) / 2.0)
    kx = math.cos(lat0) * math.pi * EARTH_RADIUS_M / 180.0
    ky = math.pi * EARTH_RADIUS_M / 180.0

    ax, ay = a.lon * kx, a.lat * ky
    bx, by = b.lon * kx, b.lat * ky
    px, py = p.lon * kx, p.lat * ky

    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = 0.0 if denom == 0 else ((px - ax) * dx + (py - ay) * dy) / denom
    t = min(1.0, max(0.0, t))

    closest = interpolate(a, b, t)
    return closest, distance_m(p, closest), t


def polyline_length_m(points: Sequence[Point]) -> float:
    return sum(distance_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def point_along_polyline(points: Sequence[Point], distance_from_start_m: float) -> Point:
    """Walk `distance_from_start_m` along a polyline and return that position."""
    if not points:
        raise ValueError("empty polyline")
    if distance_from_start_m <= 0 or len(points) == 1:
        return points[0]
    travelled = 0.0
    for i in range(len(points) - 1):
        seg = distance_m(points[i], points[i + 1])
        if travelled + seg >= distance_from_start_m:
            remaining = distance_from_start_m - travelled
            return interpolate(points[i], points[i + 1], remaining / seg if seg else 0.0)
        travelled += seg
    return points[-1]


def distance_to_polyline_m(p: Point, points: Sequence[Point]) -> tuple[float, float]:
    """Return ``(perpendicular_distance_m, distance_along_polyline_m)``."""
    best_d = float("inf")
    best_along = 0.0
    travelled = 0.0
    for i in range(len(points) - 1):
        _, d, t = project_on_segment(p, points[i], points[i + 1])
        seg_len = distance_m(points[i], points[i + 1])
        if d < best_d:
            best_d = d
            best_along = travelled + seg_len * t
        travelled += seg_len
    if not points[1:]:
        return distance_m(p, points[0]), 0.0
    return best_d, best_along


def geohash(lat: float, lon: float, precision: int = 6) -> str:
    """Encode a coordinate as a geohash.

    Precision 6 gives ~1.2 km x 0.6 km cells - the shard size used for
    broadcasting Layer 4 driver alerts to nearby road users.
    """
    lat_range, lon_range = [-90.0, 90.0], [-180.0, 180.0]
    out, bit, ch, even = [], 0, 0, True
    while len(out) < precision:
        if even:
            mid = sum(lon_range) / 2
            if lon > mid:
                ch = (ch << 1) | 1
                lon_range[0] = mid
            else:
                ch <<= 1
                lon_range[1] = mid
        else:
            mid = sum(lat_range) / 2
            if lat > mid:
                ch = (ch << 1) | 1
                lat_range[0] = mid
            else:
                ch <<= 1
                lat_range[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_BASE32[ch])
            bit, ch = 0, 0
    return "".join(out)


def geohash_neighbours(lat: float, lon: float, precision: int = 6) -> list[str]:
    """The geohash cell containing the point plus its eight neighbours.

    Subscribing a driver to all nine cells removes the edge case where an
    alert is missed because the vehicle sits near a cell boundary.
    """
    # Cell size at this precision, derived rather than hard-coded.
    lat_bits = (precision * 5) // 2
    lon_bits = precision * 5 - lat_bits
    dlat = 180.0 / (2**lat_bits)
    dlon = 360.0 / (2**lon_bits)
    cells = {
        geohash(lat + i * dlat, lon + j * dlon, precision)
        for i in (-1, 0, 1)
        for j in (-1, 0, 1)
    }
    return sorted(cells)


def polyline_to_geojson(points: Iterable[Point]) -> dict:
    return {"type": "LineString", "coordinates": [p.as_geojson() for p in points]}


def kmh_to_ms(kmh: float) -> float:
    return kmh / 3.6


def ms_to_kmh(ms: float) -> float:
    return ms * 3.6
