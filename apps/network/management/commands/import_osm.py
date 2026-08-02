"""Import a real road network from OpenStreetMap via the Overpass API.

Replaces the synthetic grid from ``seed_demo`` with the actual streets of a
city, so a pilot routes on real geometry.  Signals are created from OSM's own
``highway=traffic_signals`` nodes, which is how the city's junctions are
already mapped.

    python manage.py import_osm --bbox 13.00 80.20 13.12 80.30
    python manage.py import_osm --city Coimbatore --radius 8000

Requires internet access; Overpass is rate-limited, so keep areas modest.
"""
from __future__ import annotations

import time

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.enums import FREE_FLOW_KMH, RoadClass
from apps.core.geo import haversine_m, polyline_length_m, Point
from apps.network.models import Intersection, RoadSegment, TrafficSignal

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

#: OSM highway values SEVPS routes on, mapped to its own road classes.
HIGHWAY_MAP = {
    "motorway": RoadClass.MOTORWAY, "motorway_link": RoadClass.MOTORWAY,
    "trunk": RoadClass.TRUNK, "trunk_link": RoadClass.TRUNK,
    "primary": RoadClass.PRIMARY, "primary_link": RoadClass.PRIMARY,
    "secondary": RoadClass.SECONDARY, "secondary_link": RoadClass.SECONDARY,
    "tertiary": RoadClass.TERTIARY, "tertiary_link": RoadClass.TERTIARY,
    "residential": RoadClass.RESIDENTIAL, "unclassified": RoadClass.RESIDENTIAL,
    "living_street": RoadClass.RESIDENTIAL, "service": RoadClass.SERVICE,
}


class Command(BaseCommand):
    help = "Import a routable road network from OpenStreetMap."

    def add_arguments(self, parser):
        parser.add_argument("--bbox", nargs=4, type=float, metavar=("S", "W", "N", "E"))
        parser.add_argument("--city", type=str, help="Geocode this place and import around it.")
        parser.add_argument("--radius", type=int, default=6000, help="Metres, with --city.")
        parser.add_argument("--reset", action="store_true", help="Delete the existing network.")
        parser.add_argument("--timeout", type=int, default=180)

    def handle(self, *args, **options):
        bbox = options["bbox"]
        if not bbox:
            if not options["city"]:
                raise CommandError("Provide --bbox S W N E or --city NAME.")
            bbox = self._geocode_bbox(options["city"], options["radius"])

        south, west, north, east = bbox
        self.stdout.write(f"Querying Overpass for bbox {south},{west},{north},{east} ...")
        data = self._query_overpass(south, west, north, east, options["timeout"])
        self._import(data, options["reset"])

    # ------------------------------------------------------------------ fetch
    def _geocode_bbox(self, city: str, radius_m: int) -> tuple[float, float, float, float]:
        response = requests.get(
            NOMINATIM_URL,
            params={"q": city, "format": "json", "limit": 1},
            headers={"User-Agent": "SEVPS/1.0 (emergency routing pilot)"},
            timeout=30,
        )
        response.raise_for_status()
        results = response.json()
        if not results:
            raise CommandError(f"Could not geocode '{city}'.")
        lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
        from apps.core.geo import bounding_box

        return bounding_box(lat, lon, radius_m)

    def _query_overpass(self, south, west, north, east, timeout) -> dict:
        query = f"""
        [out:json][timeout:{timeout}];
        (
          way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|living_street)(_link)?$"]
             ({south},{west},{north},{east});
          node["highway"="traffic_signals"]({south},{west},{north},{east});
        );
        out body;
        >;
        out skel qt;
        """
        for attempt in range(3):
            response = requests.post(OVERPASS_URL, data={"data": query}, timeout=timeout + 30)
            if response.status_code == 200:
                return response.json()
            if response.status_code in (429, 504):
                wait = 15 * (attempt + 1)
                self.stdout.write(self.style.WARNING(f"  Overpass busy; retrying in {wait}s"))
                time.sleep(wait)
                continue
            response.raise_for_status()
        raise CommandError("Overpass API did not respond successfully after 3 attempts.")

    # ----------------------------------------------------------------- import
    @transaction.atomic
    def _import(self, data: dict, reset: bool) -> None:
        if reset:
            TrafficSignal.objects.all().delete()
            RoadSegment.objects.all().delete()
            Intersection.objects.all().delete()

        elements = data.get("elements", [])
        nodes = {e["id"]: e for e in elements if e["type"] == "node"}
        ways = [e for e in elements if e["type"] == "way" and "highway" in e.get("tags", {})]
        signal_nodes = {
            e["id"] for e in elements
            if e["type"] == "node" and e.get("tags", {}).get("highway") == "traffic_signals"
        }

        # A node is a routing intersection if it is shared by 2+ ways, is an
        # endpoint, or carries signals.  Interior shape points become geometry.
        usage: dict[int, int] = {}
        for way in ways:
            for node_id in way.get("nodes", []):
                usage[node_id] = usage.get(node_id, 0) + 1

        junction_ids = set()
        for way in ways:
            way_nodes = way.get("nodes", [])
            if not way_nodes:
                continue
            junction_ids.add(way_nodes[0])
            junction_ids.add(way_nodes[-1])
            junction_ids.update(n for n in way_nodes if usage.get(n, 0) > 1)
        junction_ids.update(signal_nodes & usage.keys())

        Intersection.objects.bulk_create(
            [
                Intersection(
                    osm_id=node_id,
                    latitude=nodes[node_id]["lat"],
                    longitude=nodes[node_id]["lon"],
                    is_signalised=node_id in signal_nodes,
                    base_delay_s=22.0 if node_id in signal_nodes else 3.0,
                )
                for node_id in junction_ids
                if node_id in nodes
            ],
            batch_size=1000,
            ignore_conflicts=True,
        )
        by_osm = {i.osm_id: i for i in Intersection.objects.filter(osm_id__in=junction_ids)}
        self.stdout.write(f"  {len(by_osm)} intersections")

        segments = []
        for way in ways:
            tags = way.get("tags", {})
            road_class = HIGHWAY_MAP.get(tags.get("highway"), RoadClass.RESIDENTIAL)
            name = tags.get("name", tags.get("ref", ""))[:160]
            lanes = self._int_tag(tags.get("lanes"), default=2)
            speed = self._speed_tag(tags.get("maxspeed"), road_class)
            oneway = tags.get("oneway") in {"yes", "true", "1"} or tags.get("junction") == "roundabout"

            way_nodes = [n for n in way.get("nodes", []) if n in nodes]
            # Split the way at each junction into routable segments.
            chunk: list[int] = []
            for node_id in way_nodes:
                chunk.append(node_id)
                if len(chunk) > 1 and node_id in by_osm:
                    segments.extend(
                        self._build_segments(chunk, nodes, by_osm, name, road_class, lanes, speed, oneway)
                    )
                    chunk = [node_id]

        RoadSegment.objects.bulk_create(segments, batch_size=1000, ignore_conflicts=True)
        self.stdout.write(f"  {len(segments)} directed segments")

        signals = [
            TrafficSignal(
                intersection=intersection,
                controller_id=f"OSM-{intersection.osm_id}",
                controller_type="simulated",
                cycle_seconds=120,
                supports_preemption=True,
            )
            for intersection in Intersection.objects.filter(is_signalised=True, signal__isnull=True)
        ]
        TrafficSignal.objects.bulk_create(signals, batch_size=500, ignore_conflicts=True)
        self.stdout.write(f"  {len(signals)} traffic signals")

        from apps.brain import graph as graph_mod

        graph_mod.invalidate()
        self.stdout.write(self.style.SUCCESS("OpenStreetMap import complete."))

    def _build_segments(self, chunk, nodes, by_osm, name, road_class, lanes, speed, oneway):
        start, end = by_osm.get(chunk[0]), by_osm.get(chunk[-1])
        if start is None or end is None or start.id == end.id:
            return []

        shape = [[nodes[n]["lat"], nodes[n]["lon"]] for n in chunk]
        length = polyline_length_m([Point(lat, lon) for lat, lon in shape])
        if length < 5:
            return []
        mid = shape[len(shape) // 2]

        def make(a, b, geometry):
            return RoadSegment(
                from_node=a, to_node=b, name=name, road_class=road_class,
                length_m=round(length, 1), lanes=lanes, free_flow_kmh=speed,
                geometry=geometry, latitude=mid[0], longitude=mid[1],
                allows_contraflow=road_class in {RoadClass.PRIMARY, RoadClass.SECONDARY, RoadClass.TRUNK},
            )

        rows = [make(start, end, shape)]
        if not oneway:
            rows.append(make(end, start, list(reversed(shape))))
        return rows

    @staticmethod
    def _int_tag(value, default: int) -> int:
        try:
            return max(1, min(8, int(str(value).split(";")[0])))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _speed_tag(value, road_class) -> float:
        if value:
            try:
                if "mph" in str(value):
                    return float(str(value).replace("mph", "").strip()) * 1.60934
                return float(str(value).split(";")[0])
            except ValueError:
                pass
        return FREE_FLOW_KMH[road_class]
