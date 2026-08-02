"""GIS layer builders - one GeoJSON contract for every map layer.

Before this, each screen assembled its own map data from whatever endpoint was
nearest: the console called ``/segments/geojson/`` for roads but built hospital
and signal markers from paginated model serializers, converting shapes by hand
in three places. Adding a layer meant touching the API, the types and the page.

A layer here is a named function returning a GeoJSON ``FeatureCollection`` with
a documented property set. Any client - the React console, a QGIS import, a
smart-city display, a third-party navigation integration - consumes the same
bytes, and adding a layer means adding one entry to :data:`LAYERS`.

Coordinate order is GeoJSON's ``[lon, lat]`` throughout, without exception.
SEVPS APIs elsewhere return ``[lat, lon]`` because Leaflet wants that, and
mixing the two silently is the single most common way to put an ambulance in
the sea. Anything in this module is spec-compliant; conversion happens once, in
the client's map component.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from django.conf import settings
from django.utils import timezone

log = logging.getLogger("sevps.gis")


# ---------------------------------------------------------------------------
# GeoJSON helpers
# ---------------------------------------------------------------------------
def feature(geometry: dict, properties: dict, feature_id=None) -> dict:
    payload = {"type": "Feature", "geometry": geometry, "properties": properties}
    if feature_id is not None:
        payload["id"] = feature_id
    return payload


def point(longitude: float, latitude: float) -> dict:
    return {"type": "Point", "coordinates": [longitude, latitude]}


def line(coordinates: list[list[float]]) -> dict:
    """``coordinates`` must already be [lon, lat] pairs."""
    return {"type": "LineString", "coordinates": coordinates}


def collection(features: list[dict], **metadata) -> dict:
    return {
        "type": "FeatureCollection",
        "features": features,
        # Non-standard but harmless, and every consumer wants it: how fresh
        # the layer is and how much of it was returned.
        "metadata": {"generated_at": timezone.now().isoformat(), "count": len(features), **metadata},
    }


def latlon_pairs_to_geojson(pairs) -> list[list[float]]:
    """``[[lat, lon], ...]`` -> ``[[lon, lat], ...]``. The only place this flips."""
    return [[float(lon), float(lat)] for lat, lon in pairs or []]


# ---------------------------------------------------------------------------
# Layer builders
# ---------------------------------------------------------------------------
def road_network(limit: int = 4000, **_) -> dict:
    """The routable graph, coloured by live congestion."""
    from apps.network.models import RoadSegment

    features = [
        feature(
            line(segment.geojson_coordinates()),
            {
                "name": segment.name,
                "road_class": segment.road_class,
                "lanes": segment.lanes,
                "congestion_level": segment.congestion_level,
                "congestion_index": round(segment.congestion_index, 3),
                "speed_kmh": segment.current_speed_kmh,
                "free_flow_kmh": segment.design_speed_kmh,
                "is_open": segment.is_open,
            },
            segment.id,
        )
        for segment in RoadSegment.objects.all()[:limit]
    ]
    return collection(features, layer="road_network", truncated=len(features) >= limit)


def hospitals(**_) -> dict:
    """Receiving facilities, with the capability and capacity a router needs."""
    from apps.hospitals.models import Hospital

    features = []
    for hospital in (
        Hospital.objects.filter(is_active=True)
        .prefetch_related("capabilities")
        .select_related("capacity_row")
    ):
        capacity = hospital.capacity
        features.append(
            feature(
                point(hospital.longitude, hospital.latitude),
                {
                    "code": hospital.code,
                    "name": hospital.name,
                    "is_on_diversion": hospital.is_on_diversion,
                    "diversion_reason": hospital.diversion_reason,
                    "is_trauma_designated": hospital.is_trauma_designated,
                    "facilities": sorted(hospital.facility_codes),
                    "emergency_beds_available": capacity.emergency_beds_available,
                    "icu_beds_available": capacity.icu_beds_available,
                    "workload_index": capacity.workload_index,
                    "capacity_is_stale": capacity.is_stale,
                    "phone": hospital.emergency_phone or hospital.phone,
                },
                hospital.id,
            )
        )
    return collection(features, layer="hospitals")


def traffic_signals(**_) -> dict:
    """Signal locations and whether each is currently held for a corridor."""
    from apps.network.models import TrafficSignal

    features = [
        feature(
            point(signal.intersection.longitude, signal.intersection.latitude),
            {
                "controller_id": signal.controller_id,
                "intersection": signal.intersection.label,
                "current_phase": signal.current_phase,
                "is_preempted": signal.is_preempted,
                "supports_preemption": signal.supports_preemption,
                "is_online": signal.is_online,
                "cycle_seconds": signal.cycle_seconds,
            },
            signal.id,
        )
        for signal in TrafficSignal.objects.select_related("intersection")
    ]
    return collection(features, layer="traffic_signals")


def road_closures(**_) -> dict:
    """Active disruptions.

    Blocking events are separated from merely-slow ones in the properties,
    because a client should render "avoid" differently from "expect delay" -
    and because the router treats them differently too.
    """
    from apps.network.models import RoadEvent

    features = [
        feature(
            point(event.longitude, event.latitude),
            {
                "event_type": event.event_type,
                "event_type_display": event.get_event_type_display(),
                "description": event.description,
                "severity": round(event.severity, 3),
                "confidence": round(event.confidence, 3),
                "blocks_road": event.blocks_road,
                "source": event.source,
                "radius_m": event.radius_m,
                "starts_at": event.starts_at.isoformat(),
                "ends_at": event.ends_at.isoformat() if event.ends_at else None,
            },
            event.id,
        )
        for event in RoadEvent.objects.active().select_related("segment")
    ]
    return collection(features, layer="road_closures")


def emergency_routes(**_) -> dict:
    """Active green-corridor routes as lines."""
    from apps.dispatch.models import EmergencyTrip

    features = []
    for trip in (
        EmergencyTrip.objects.active()
        .select_related("vehicle", "destination_hospital")
        .prefetch_related("routes")
    ):
        plan = trip.active_route
        if plan is None or len(plan.geometry or []) < 2:
            continue
        features.append(
            feature(
                line(latlon_pairs_to_geojson(plan.geometry)),
                {
                    "trip_id": trip.id,
                    "reference": trip.reference,
                    "vehicle": trip.vehicle.callsign,
                    "priority_level": trip.priority_level,
                    "stage": trip.stage,
                    "hospital": (
                        trip.destination_hospital.name if trip.destination_hospital_id else None
                    ),
                    "distance_m": round(plan.total_distance_m, 1),
                    "duration_s": round(plan.total_duration_s, 1),
                    "eta": plan.predicted_eta.isoformat() if plan.predicted_eta else None,
                    # Deliberately no clinical fields: this layer is consumed by
                    # traffic-side clients that have no clearance for them.
                },
                plan.id,
            )
        )
    return collection(features, layer="emergency_routes")


def emergency_vehicles(**_) -> dict:
    """Live fleet positions."""
    from apps.fleet.models import EmergencyVehicle

    features = [
        feature(
            point(vehicle.longitude, vehicle.latitude),
            {
                "callsign": vehicle.callsign,
                "vehicle_type": vehicle.vehicle_type,
                "status": vehicle.status,
                "priority_level": vehicle.priority_level,
                "siren_mode": vehicle.siren_mode,
                "heading_deg": round(vehicle.heading_deg, 1),
                "speed_kmh": round(vehicle.speed_kmh, 1),
                "is_stale": vehicle.is_stale,
            },
            vehicle.id,
        )
        for vehicle in EmergencyVehicle.objects.online()
    ]
    return collection(features, layer="emergency_vehicles")


def display_boards(**_) -> dict:
    from apps.alerts.models import DisplayBoard

    features = [
        feature(
            point(board.longitude, board.latitude),
            {
                "code": board.code,
                "name": board.name,
                "channel": board.channel,
                "message": board.current_message if board.is_displaying_alert else "",
                "is_displaying_alert": board.is_displaying_alert,
            },
            board.id,
        )
        for board in DisplayBoard.objects.filter(is_active=True)
    ]
    return collection(features, layer="display_boards")


def cameras(**_) -> dict:
    from apps.network.models import CameraFeed

    features = [
        feature(
            point(camera.longitude, camera.latitude),
            {
                "name": camera.name,
                "heading_deg": round(camera.heading_deg, 1),
                "is_active": camera.is_active,
                "last_analysed_at": (
                    camera.last_analysed_at.isoformat() if camera.last_analysed_at else None
                ),
            },
            camera.id,
        )
        for camera in CameraFeed.objects.filter(is_active=True)
    ]
    return collection(features, layer="cameras")


# ---------------------------------------------------------------------------
# Heatmaps
# ---------------------------------------------------------------------------
#: Heatmaps are weighted points, not polygons. Returning raw weighted points
#: and letting the client bin them keeps the payload small and lets the same
#: data drive different visualisations at different zoom levels.
def congestion_heatmap(**_) -> dict:
    """Where the network is slow right now, weighted by how slow."""
    from apps.network.models import RoadSegment

    features = [
        feature(
            point(segment.longitude, segment.latitude),
            {
                "weight": round(segment.congestion_index, 3),
                "congestion_level": segment.congestion_level,
                "name": segment.name,
                "speed_kmh": segment.current_speed_kmh,
            },
            segment.id,
        )
        for segment in RoadSegment.objects.filter(congestion_index__gt=0.15).only(
            "id", "name", "latitude", "longitude",
            "congestion_index", "congestion_level", "current_speed_kmh",
        )
    ]
    return collection(features, layer="congestion_heatmap", weight_field="weight")


def accident_heatmap(window_days: int = 180, **_) -> dict:
    """Historical accident density, from the clustered hotspots (feature 4.9)."""
    from apps.analytics.models import Hotspot

    features = [
        feature(
            point(hotspot.longitude, hotspot.latitude),
            {
                "weight": round(hotspot.score, 3),
                "label": hotspot.label,
                "incident_count": hotspot.incident_count,
                "window_days": hotspot.window_days,
            },
            hotspot.id,
        )
        for hotspot in Hotspot.objects.filter(kind=Hotspot.Kind.ACCIDENT)
    ]
    return collection(features, layer="accident_heatmap", weight_field="weight")


def delay_heatmap(days: int = 30, **_) -> dict:
    """Where emergency vehicles actually lose time.

    Distinct from the congestion heatmap: a road can be slow for everyone yet
    cost an ambulance nothing if the corridor works, while a junction with a
    failing controller costs time without looking congested at all.
    """
    from apps.analytics.services import high_delay_intersections

    rows = high_delay_intersections(days=days, limit=100)
    if not rows:
        return collection([], layer="delay_heatmap", weight_field="weight")

    worst = max(row["score"] for row in rows) or 1.0
    features = [
        feature(
            point(row["longitude"], row["latitude"]),
            {
                "weight": round(row["score"] / worst, 3),
                "name": row["name"],
                "controller_id": row["controller_id"],
                "avg_clearance_s": row["avg_clearance_s"],
                "avg_eta_error_s": row["avg_eta_error_s"],
                "events": row["events"],
            },
            row["intersection_id"],
        )
        for row in rows
    ]
    return collection(features, layer="delay_heatmap", weight_field="weight")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LayerSpec:
    name: str
    builder: Callable[..., dict]
    title: str
    description: str
    geometry: str
    #: Layers carrying operational detail require a signed-in role; base
    #: geography and public infrastructure do not.
    public: bool = False
    #: Rendered as a weighted heat surface rather than discrete features.
    is_heatmap: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "geometry": self.geometry,
            "public": self.public,
            "is_heatmap": self.is_heatmap,
            "url": f"/api/v1/network/gis/layers/{self.name}/",
        }


LAYERS: dict[str, LayerSpec] = {
    spec.name: spec
    for spec in (
        LayerSpec("road_network", road_network, "Road network",
                  "Routable graph coloured by live congestion.", "LineString", public=True),
        LayerSpec("hospitals", hospitals, "Hospitals",
                  "Receiving facilities with capability and live capacity.", "Point", public=True),
        LayerSpec("traffic_signals", traffic_signals, "Traffic signals",
                  "Signal locations and current preemption state.", "Point", public=True),
        LayerSpec("road_closures", road_closures, "Road closures & disruptions",
                  "Active accidents, closures, waterlogging and events.", "Point", public=True),
        LayerSpec("cameras", cameras, "Traffic cameras",
                  "Camera estate used for computer-vision analysis.", "Point", public=True),
        LayerSpec("display_boards", display_boards, "Display boards",
                  "Roadside variable-message signs and what they show.", "Point", public=True),
        LayerSpec("emergency_routes", emergency_routes, "Emergency routes",
                  "Active green-corridor routes.", "LineString"),
        LayerSpec("emergency_vehicles", emergency_vehicles, "Emergency vehicles",
                  "Live fleet positions and siren state.", "Point"),
        LayerSpec("congestion_heatmap", congestion_heatmap, "Congestion heatmap",
                  "Where the network is slow right now.", "Point",
                  public=True, is_heatmap=True),
        LayerSpec("accident_heatmap", accident_heatmap, "Accident hotspots",
                  "Historical accident density (feature 4.9).", "Point", is_heatmap=True),
        LayerSpec("delay_heatmap", delay_heatmap, "Emergency delay hotspots",
                  "Junctions where emergency vehicles lose the most time.", "Point",
                  is_heatmap=True),
    )
}


def build(name: str, **options) -> dict:
    spec = LAYERS.get(name)
    if spec is None:
        raise KeyError(name)
    return spec.builder(**options)


def catalogue() -> list[dict]:
    return [spec.as_dict() for spec in LAYERS.values()]


# ---------------------------------------------------------------------------
# Basemap providers
# ---------------------------------------------------------------------------
def basemap_providers() -> dict:
    """Which basemaps this deployment can actually serve.

    OpenStreetMap through CARTO is the default and needs no key, which matters:
    an emergency platform should not have a hard dependency on a commercial
    tile contract to draw a map. Mapbox is enabled when a token is configured
    and adds a live traffic overlay OSM has no equivalent for. Google Maps is
    intentionally left as configuration only - it duplicates what the other two
    already provide, and its terms are the most restrictive of the three.
    """
    config = settings.SEVPS
    mapbox_token = config.get("MAPBOX_TOKEN", "")
    google_key = config.get("GOOGLE_MAPS_KEY", "")

    providers = [
        {
            "id": "carto_dark",
            "name": "OpenStreetMap (CARTO dark)",
            "url": "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
            "attribution": "&copy; OpenStreetMap contributors &copy; CARTO",
            "subdomains": "abcd",
            "max_zoom": 20,
            "default": True,
            "requires_key": False,
        },
        {
            "id": "osm",
            "name": "OpenStreetMap (standard)",
            "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
            "attribution": "&copy; OpenStreetMap contributors",
            "subdomains": "abc",
            "max_zoom": 19,
            "default": False,
            "requires_key": False,
        },
    ]

    if mapbox_token:
        providers.extend(
            [
                {
                    "id": "mapbox_dark",
                    "name": "Mapbox Dark",
                    "url": (
                        "https://api.mapbox.com/styles/v1/mapbox/dark-v11/tiles/"
                        "{z}/{x}/{y}?access_token=" + mapbox_token
                    ),
                    "attribution": "&copy; Mapbox &copy; OpenStreetMap",
                    "max_zoom": 22,
                    "default": False,
                    "requires_key": True,
                },
                {
                    "id": "mapbox_traffic",
                    "name": "Mapbox Traffic",
                    "url": (
                        "https://api.mapbox.com/styles/v1/mapbox/navigation-night-v1/tiles/"
                        "{z}/{x}/{y}?access_token=" + mapbox_token
                    ),
                    "attribution": "&copy; Mapbox &copy; OpenStreetMap",
                    "max_zoom": 22,
                    "default": False,
                    "requires_key": True,
                    # Mapbox's own live traffic, which SEVPS renders *beside*
                    # its own congestion layer rather than instead of it -
                    # they are independent sources and disagreement is signal.
                    "is_traffic": True,
                },
            ]
        )

    return {
        "providers": providers,
        "default": next(p["id"] for p in providers if p["default"]),
        "mapbox_available": bool(mapbox_token),
        "google_maps_available": bool(google_key),
        "google_maps_note": (
            "Google Maps is supported as configuration only. It duplicates the "
            "basemap and traffic coverage Mapbox and OSM already provide, and "
            "carries the most restrictive terms of the three, so SEVPS does not "
            "render it by default."
        ),
    }
