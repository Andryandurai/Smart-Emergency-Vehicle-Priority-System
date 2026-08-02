"""The routable city graph.

Topology (which node connects to which) changes rarely; traffic state changes
every few seconds.  So SEVPS caches the *topology* as a NetworkX DiGraph and
re-reads only the *state* on each request.  Route search then evaluates edge
cost lazily against a :class:`~apps.brain.congestion.CongestionForecaster`,
which is what makes the search time-dependent.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import networkx as nx
from django.utils import timezone

from apps.core.geo import Point, haversine_m

log = logging.getLogger("sevps.brain.graph")

#: Topology cache lifetime.  Road networks are edited by operators, not by
#: traffic, so a minute of staleness is harmless and saves a lot of queries.
TOPOLOGY_TTL_S = 60.0
#: Live state cache lifetime - short, because this is the fast-moving part.
STATE_TTL_S = 5.0

_lock = threading.RLock()
_topology: "Topology | None" = None
_state: "NetworkState | None" = None


@dataclass
class EdgeState:
    """Everything the cost function needs about one directed segment."""

    segment_id: int
    from_node: int
    to_node: int
    name: str
    road_class: str
    length_m: float
    design_kmh: float
    lanes: int
    live_kmh: float | None
    live_age_s: float | None
    congestion_index: float
    is_open: bool
    allows_contraflow: bool


@dataclass
class Topology:
    graph: nx.DiGraph
    node_points: dict[int, Point]
    node_delay_s: dict[int, float]
    signal_nodes: set[int]
    built_at: float
    #: Fastest design speed anywhere in the network (m/s) - the A* heuristic
    #: divides by this so it stays admissible (never overestimates).
    max_speed_ms: float


@dataclass
class NetworkState:
    edges: dict[int, EdgeState]              # segment_id -> state
    by_node_pair: dict[tuple[int, int], int]  # (u, v) -> segment_id
    built_at: float


def invalidate() -> None:
    """Drop both caches - call after editing the network."""
    global _topology, _state
    with _lock:
        _topology = None
        _state = None


def _build_topology() -> Topology:
    from apps.network.models import Intersection, RoadSegment

    graph = nx.DiGraph()
    node_points: dict[int, Point] = {}
    node_delay: dict[int, float] = {}
    signal_nodes: set[int] = set()

    for node in Intersection.objects.all().only(
        "id", "latitude", "longitude", "base_delay_s", "is_signalised"
    ):
        node_points[node.id] = Point(node.latitude, node.longitude)
        node_delay[node.id] = node.base_delay_s
        if node.is_signalised:
            signal_nodes.add(node.id)
        graph.add_node(node.id)

    max_design = 1.0
    for seg in RoadSegment.objects.all().only(
        "id", "from_node_id", "to_node_id", "length_m", "free_flow_kmh", "road_class"
    ):
        if seg.from_node_id not in node_points or seg.to_node_id not in node_points:
            continue
        graph.add_edge(seg.from_node_id, seg.to_node_id, segment_id=seg.id)
        max_design = max(max_design, seg.design_speed_kmh)

    log.info(
        "Rebuilt routing topology: %d nodes, %d edges", graph.number_of_nodes(), graph.number_of_edges()
    )
    return Topology(
        graph=graph,
        node_points=node_points,
        node_delay_s=node_delay,
        signal_nodes=signal_nodes,
        built_at=time.monotonic(),
        max_speed_ms=(max_design * 1.25) / 3.6,
    )


def _build_state() -> NetworkState:
    from apps.network.models import RoadSegment

    now = timezone.now()
    edges: dict[int, EdgeState] = {}
    by_pair: dict[tuple[int, int], int] = {}

    for seg in RoadSegment.objects.all().only(
        "id", "from_node_id", "to_node_id", "name", "road_class", "length_m",
        "free_flow_kmh", "lanes", "current_speed_kmh", "speed_updated_at",
        "congestion_index", "is_open", "allows_contraflow",
    ):
        age = (now - seg.speed_updated_at).total_seconds() if seg.speed_updated_at else None
        edges[seg.id] = EdgeState(
            segment_id=seg.id,
            from_node=seg.from_node_id,
            to_node=seg.to_node_id,
            name=seg.name,
            road_class=seg.road_class,
            length_m=seg.length_m,
            design_kmh=seg.design_speed_kmh,
            lanes=seg.lanes,
            live_kmh=seg.current_speed_kmh,
            live_age_s=age,
            congestion_index=seg.congestion_index,
            is_open=seg.is_open,
            allows_contraflow=seg.allows_contraflow,
        )
        by_pair[(seg.from_node_id, seg.to_node_id)] = seg.id

    return NetworkState(edges=edges, by_node_pair=by_pair, built_at=time.monotonic())


def get_topology(force: bool = False) -> Topology:
    global _topology
    with _lock:
        if force or _topology is None or (time.monotonic() - _topology.built_at) > TOPOLOGY_TTL_S:
            _topology = _build_topology()
        return _topology


def get_state(force: bool = False) -> NetworkState:
    global _state
    with _lock:
        if force or _state is None or (time.monotonic() - _state.built_at) > STATE_TTL_S:
            _state = _build_state()
        return _state


def nearest_node(lat: float, lon: float, *, topology: Topology | None = None) -> int | None:
    """Snap an arbitrary coordinate onto the graph.

    Uses an expanding-radius scan so the common case (a point right on the
    network) costs one small bounding-box pass rather than a full sweep.
    """
    topo = topology or get_topology()
    if not topo.node_points:
        return None

    for radius_deg in (0.005, 0.02, 0.08, 0.4):
        best, best_d = None, float("inf")
        for node_id, p in topo.node_points.items():
            if abs(p.lat - lat) > radius_deg or abs(p.lon - lon) > radius_deg:
                continue
            d = haversine_m(lat, lon, p.lat, p.lon)
            if d < best_d:
                best, best_d = node_id, d
        if best is not None:
            return best

    return min(
        topo.node_points,
        key=lambda n: haversine_m(lat, lon, topo.node_points[n].lat, topo.node_points[n].lon),
    )


def nearest_nodes(lat: float, lon: float, limit: int = 3) -> list[tuple[int, float]]:
    """The ``limit`` closest graph nodes with their distances, nearest first."""
    topo = get_topology()
    scored = [
        (node_id, haversine_m(lat, lon, p.lat, p.lon)) for node_id, p in topo.node_points.items()
    ]
    scored.sort(key=lambda item: item[1])
    return scored[:limit]


def segment_between(u: int, v: int) -> EdgeState | None:
    state = get_state()
    segment_id = state.by_node_pair.get((u, v))
    return state.edges.get(segment_id) if segment_id else None


def graph_summary() -> dict:
    topo = get_topology()
    state = get_state()
    blocked = sum(1 for e in state.edges.values() if not e.is_open)
    return {
        "nodes": topo.graph.number_of_nodes(),
        "edges": topo.graph.number_of_edges(),
        "signalised_nodes": len(topo.signal_nodes),
        "blocked_segments": blocked,
        "topology_age_s": round(time.monotonic() - topo.built_at, 1),
        "state_age_s": round(time.monotonic() - state.built_at, 1),
        "is_strongly_connected": (
            nx.is_strongly_connected(topo.graph) if topo.graph.number_of_nodes() else False
        ),
    }
