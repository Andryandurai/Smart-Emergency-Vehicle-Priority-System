"""Time-dependent route optimisation (Layer 2, features 4.2 / 4.10).

Ordinary shortest-path routing asks "which road is shortest?".  SEVPS asks
"which road will be fastest *at the moment the ambulance reaches it*", so the
cost of an edge depends on the arrival time at its tail node.  That rules out
NetworkX's stock ``astar_path`` (its weight callback cannot see accumulated
time), so the search below carries arrival time in the label.

Two algorithms, both operating on the same time-dependent cost:

* ``astar``     - A* with a straight-line/max-speed heuristic.  Admissible,
                  so it returns the same optimal path as Dijkstra but expands
                  far fewer nodes.  Default.
* ``dijkstra``  - uniform-cost search; used as the verification baseline and
                  when a heuristic would be misleading (heavy contraflow use).
"""
from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from apps.core.enums import EMERGENCY_SPEED_ADVANTAGE, PriorityLevel
from apps.core.geo import Point, haversine_m, polyline_length_m
from apps.brain import graph as graph_mod
from apps.brain.congestion import CongestionForecaster, blocked_segment_ids, build_forecaster

log = logging.getLogger("sevps.brain.router")

#: Guard against pathological searches on a badly imported network.
MAX_EXPANSIONS = 250_000


class RouteNotFound(Exception):
    """No path exists between the requested points under the given constraints."""


@dataclass
class RouteStep:
    """One directed segment of a computed route."""

    segment_id: int
    from_node: int
    to_node: int
    name: str
    length_m: float
    travel_time_s: float
    #: Seconds from departure at which the vehicle *enters* this segment.
    enter_offset_s: float
    #: Seconds from departure at which it *leaves* (enters the next node).
    exit_offset_s: float
    predicted_speed_kmh: float
    forecast_basis: str
    is_signalised_exit: bool

    def as_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "name": self.name,
            "length_m": round(self.length_m, 1),
            "travel_time_s": round(self.travel_time_s, 1),
            "enter_offset_s": round(self.enter_offset_s, 1),
            "exit_offset_s": round(self.exit_offset_s, 1),
            "predicted_speed_kmh": round(self.predicted_speed_kmh, 1),
            "forecast_basis": self.forecast_basis,
            "is_signalised_exit": self.is_signalised_exit,
        }


@dataclass
class Route:
    """A complete optimised route with per-intersection arrival predictions."""

    nodes: list[int]
    steps: list[RouteStep]
    total_distance_m: float
    total_duration_s: float
    algorithm: str
    departure_at: object
    origin: Point
    destination: Point
    nodes_expanded: int = 0
    geometry: list[list[float]] = field(default_factory=list)  # [[lat, lon], ...]

    @property
    def eta(self):
        return self.departure_at + timezone.timedelta(seconds=self.total_duration_s)

    @property
    def signalised_nodes(self) -> list[tuple[int, float]]:
        """``(intersection_id, seconds_from_departure)`` for signalised nodes.

        This is exactly the input the green-corridor planner needs.
        """
        return [
            (step.to_node, step.exit_offset_s) for step in self.steps if step.is_signalised_exit
        ]

    def node_arrival_offsets(self) -> dict[int, float]:
        """ETA at *every* node on the route ("ETA at every major intersection")."""
        offsets = {self.nodes[0]: 0.0} if self.nodes else {}
        for step in self.steps:
            offsets[step.to_node] = step.exit_offset_s
        return offsets

    def position_at(self, seconds_from_departure: float) -> Point:
        """Where the vehicle is predicted to be at a given offset."""
        if not self.geometry:
            return self.origin
        if seconds_from_departure <= 0:
            return self.origin
        if seconds_from_departure >= self.total_duration_s:
            return self.destination

        travelled_m = 0.0
        for step in self.steps:
            if step.exit_offset_s >= seconds_from_departure:
                span = max(1e-6, step.exit_offset_s - step.enter_offset_s)
                frac = (seconds_from_departure - step.enter_offset_s) / span
                travelled_m += step.length_m * max(0.0, min(1.0, frac))
                break
            travelled_m += step.length_m

        from apps.core.geo import point_along_polyline

        return point_along_polyline([Point(lat, lon) for lat, lon in self.geometry], travelled_m)

    def as_dict(self, include_geometry: bool = True) -> dict:
        data = {
            "algorithm": self.algorithm,
            "departure_at": self.departure_at,
            "eta": self.eta,
            "total_distance_m": round(self.total_distance_m, 1),
            "total_duration_s": round(self.total_duration_s, 1),
            "total_duration_min": round(self.total_duration_s / 60.0, 1),
            "nodes": self.nodes,
            "nodes_expanded": self.nodes_expanded,
            "steps": [s.as_dict() for s in self.steps],
            "signalised_nodes": [
                {"intersection_id": nid, "eta_offset_s": round(off, 1)}
                for nid, off in self.signalised_nodes
            ],
        }
        if include_geometry:
            data["geometry"] = self.geometry
            data["geojson"] = {
                "type": "LineString",
                "coordinates": [[lon, lat] for lat, lon in self.geometry],
            }
        return data


@dataclass
class RouteRequest:
    origin: Point
    destination: Point
    priority_level: int = PriorityLevel.NON_CRITICAL
    departure_at: object = None
    algorithm: str | None = None
    #: Segments the caller wants avoided on top of the globally blocked set
    #: (used by dynamic replanning to escape a route that has just failed).
    avoid_segment_ids: set[int] = field(default_factory=set)
    #: Allow the search to relax one-way restrictions on contraflow-capable
    #: links.  Only for Level 1 and only where the city has authorised it.
    allow_contraflow: bool = False


def _speed_multiplier(priority_level: int) -> float:
    return EMERGENCY_SPEED_ADVANTAGE.get(priority_level, 1.0)


def _signal_delay_s(priority_level: int, base_delay_s: float) -> float:
    """Expected wait at a signalised node.

    A Level 1 vehicle gets a green corridor, so it pays almost nothing; a
    Level 4 transport waits like everyone else.  Modelling this *inside* the
    search matters: it is why a Level 1 route may legitimately choose a road
    with more signals but higher speed, while a Level 4 route avoids them.
    """
    retained = {
        PriorityLevel.CRITICAL: 0.10,
        PriorityLevel.HIGH: 0.30,
        PriorityLevel.MODERATE: 0.65,
        PriorityLevel.NON_CRITICAL: 1.0,
    }.get(priority_level, 1.0)
    return base_delay_s * retained


def compute_route(request: RouteRequest) -> Route:
    """Run time-dependent shortest-path search and materialise the result."""
    topo = graph_mod.get_topology()
    state = graph_mod.get_state()
    if topo.graph.number_of_nodes() == 0:
        raise RouteNotFound("road network is empty - import or seed a network first")

    source = graph_mod.nearest_node(request.origin.lat, request.origin.lon, topology=topo)
    target = graph_mod.nearest_node(request.destination.lat, request.destination.lon, topology=topo)
    if source is None or target is None:
        raise RouteNotFound("could not snap origin/destination onto the road network")

    departure = request.departure_at or timezone.now()
    algorithm = (request.algorithm or settings.SEVPS["ROUTE_ALGORITHM"]).lower()
    forecaster = build_forecaster(departure=departure)

    avoid = set(request.avoid_segment_ids) | blocked_segment_ids()
    multiplier = _speed_multiplier(request.priority_level)

    path, arrival, expanded = _search(
        topo=topo,
        state=state,
        source=source,
        target=target,
        forecaster=forecaster,
        multiplier=multiplier,
        priority_level=request.priority_level,
        avoid=avoid,
        allow_contraflow=request.allow_contraflow,
        algorithm=algorithm,
    )
    if path is None:
        raise RouteNotFound(
            f"no route from node {source} to node {target} "
            f"({len(avoid)} segment(s) unavailable)"
        )

    return _materialise(
        path=path,
        total_time_s=arrival,
        topo=topo,
        state=state,
        forecaster=forecaster,
        multiplier=multiplier,
        priority_level=request.priority_level,
        request=request,
        departure=departure,
        algorithm=algorithm,
        expanded=expanded,
    )


def _search(
    *,
    topo,
    state,
    source: int,
    target: int,
    forecaster: CongestionForecaster,
    multiplier: float,
    priority_level: int,
    avoid: set[int],
    allow_contraflow: bool,
    algorithm: str,
):
    """Label-setting search where edge cost depends on arrival time.

    Returns ``(path, arrival_time_s, nodes_expanded)``.
    """
    use_astar = algorithm != "dijkstra"
    target_point = topo.node_points[target]
    max_speed_ms = topo.max_speed_ms * multiplier

    def heuristic(node: int) -> float:
        if not use_astar:
            return 0.0
        p = topo.node_points[node]
        # Straight-line distance at the highest speed physically attainable
        # anywhere in the network: never overestimates, so A* stays optimal.
        return haversine_m(p.lat, p.lon, target_point.lat, target_point.lon) / max_speed_ms

    best: dict[int, float] = {source: 0.0}
    came_from: dict[int, int] = {}
    heap: list[tuple[float, float, int]] = [(heuristic(source), 0.0, source)]
    closed: set[int] = set()
    expanded = 0

    while heap:
        _, arrived_at, node = heapq.heappop(heap)
        if node in closed:
            continue
        closed.add(node)
        expanded += 1
        if expanded > MAX_EXPANSIONS:  # pragma: no cover - safety valve
            log.error("route search exceeded %d expansions", MAX_EXPANSIONS)
            break

        if node == target:
            path = [node]
            while path[-1] != source:
                path.append(came_from[path[-1]])
            path.reverse()
            return path, arrived_at, expanded

        for neighbour in topo.graph.successors(node):
            if neighbour in closed:
                continue
            segment_id = state.by_node_pair.get((node, neighbour))
            edge = state.edges.get(segment_id) if segment_id else None
            if edge is None or segment_id in avoid:
                continue
            if not edge.is_open and not (allow_contraflow and edge.allows_contraflow):
                continue

            forecast = forecaster.forecast(edge, arrived_at)
            travel_s = edge.length_m / max(1.0, (forecast.speed_kmh * multiplier) / 3.6)
            travel_s += _signal_delay_s(priority_level, topo.node_delay_s.get(neighbour, 0.0))

            tentative = arrived_at + travel_s
            if tentative < best.get(neighbour, float("inf")):
                best[neighbour] = tentative
                came_from[neighbour] = node
                heapq.heappush(heap, (tentative + heuristic(neighbour), tentative, neighbour))

    return None, float("inf"), expanded


def _materialise(
    *, path, total_time_s, topo, state, forecaster, multiplier, priority_level,
    request, departure, algorithm, expanded,
) -> Route:
    """Turn a node path into steps, geometry and per-node arrival times."""
    from apps.network.models import RoadSegment

    segment_ids = [
        state.by_node_pair[(path[i], path[i + 1])] for i in range(len(path) - 1)
    ]
    shapes = {
        seg.id: seg.shape
        for seg in RoadSegment.objects.filter(id__in=segment_ids).only(
            "id", "geometry", "from_node_id", "to_node_id"
        ).select_related("from_node", "to_node")
    }

    steps: list[RouteStep] = []
    geometry: list[list[float]] = []
    offset = 0.0
    distance = 0.0

    for segment_id, (u, v) in zip(segment_ids, zip(path, path[1:])):
        edge = state.edges[segment_id]
        forecast = forecaster.forecast(edge, offset)
        speed_kmh = forecast.speed_kmh * multiplier
        travel_s = edge.length_m / max(1.0, speed_kmh / 3.6)
        travel_s += _signal_delay_s(priority_level, topo.node_delay_s.get(v, 0.0))

        steps.append(
            RouteStep(
                segment_id=segment_id,
                from_node=u,
                to_node=v,
                name=edge.name,
                length_m=edge.length_m,
                travel_time_s=travel_s,
                enter_offset_s=offset,
                exit_offset_s=offset + travel_s,
                predicted_speed_kmh=speed_kmh,
                forecast_basis=forecast.basis,
                is_signalised_exit=v in topo.signal_nodes,
            )
        )

        shape = shapes.get(segment_id) or [topo.node_points[u], topo.node_points[v]]
        for point in shape:
            coord = [point.lat, point.lon]
            if not geometry or geometry[-1] != coord:
                geometry.append(coord)

        offset += travel_s
        distance += edge.length_m

    # Stitch the true origin/destination onto the snapped network path so the
    # drawn route starts and ends where the vehicle and hospital actually are.
    origin_coord = [request.origin.lat, request.origin.lon]
    dest_coord = [request.destination.lat, request.destination.lon]
    if not geometry or geometry[0] != origin_coord:
        geometry.insert(0, origin_coord)
    if geometry[-1] != dest_coord:
        geometry.append(dest_coord)

    # Account for the walk-on/walk-off legs in distance and time.
    approach_m = haversine_m(
        request.origin.lat, request.origin.lon,
        topo.node_points[path[0]].lat, topo.node_points[path[0]].lon,
    )
    egress_m = haversine_m(
        topo.node_points[path[-1]].lat, topo.node_points[path[-1]].lon,
        request.destination.lat, request.destination.lon,
    )
    connector_speed_ms = 30.0 / 3.6
    distance += approach_m + egress_m
    offset += (approach_m + egress_m) / connector_speed_ms

    return Route(
        nodes=path,
        steps=steps,
        total_distance_m=distance,
        total_duration_s=offset,
        algorithm=algorithm,
        departure_at=departure,
        origin=request.origin,
        destination=request.destination,
        nodes_expanded=expanded,
        geometry=geometry,
    )


def route_between(
    origin: Point,
    destination: Point,
    *,
    priority_level: int = PriorityLevel.NON_CRITICAL,
    departure_at=None,
    algorithm: str | None = None,
    avoid_segment_ids: set[int] | None = None,
    allow_contraflow: bool = False,
) -> Route:
    """Convenience wrapper around :func:`compute_route`."""
    return compute_route(
        RouteRequest(
            origin=origin,
            destination=destination,
            priority_level=priority_level,
            departure_at=departure_at,
            algorithm=algorithm,
            avoid_segment_ids=avoid_segment_ids or set(),
            allow_contraflow=allow_contraflow,
        )
    )


def estimate_travel_time_s(origin: Point, destination: Point, priority_level: int) -> float:
    """Fast travel-time estimate, e.g. for ranking many hospital candidates.

    Falls back to a straight-line estimate when the points cannot be routed,
    so a hospital is never dropped from consideration because of a graph gap.
    """
    try:
        return route_between(origin, destination, priority_level=priority_level).total_duration_s
    except RouteNotFound:
        distance = haversine_m(origin.lat, origin.lon, destination.lat, destination.lon)
        # 1.4 detour factor is the usual road-network vs crow-flies ratio.
        return (distance * 1.4) / (35.0 / 3.6)


def route_length_m(geometry: list[list[float]]) -> float:
    return polyline_length_m([Point(lat, lon) for lat, lon in geometry])
