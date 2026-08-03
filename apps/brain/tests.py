"""AI engine tests: routing optimality, time-dependence, and replanning."""
from django.test import TestCase
from django.utils import timezone

from apps.brain import graph as graph_mod
from apps.brain.congestion import (
    CongestionForecaster,
    build_forecaster,
    congested_segment_ids,
    default_speed_factor,
)
from apps.brain.rerouting import evaluate_trip
from apps.brain.router import RouteNotFound, route_between
from apps.core.enums import (
    CongestionLevel,
    PriorityLevel,
    RoadClass,
    TripStage,
    VehicleStatus,
)
from apps.core.geo import Point
from apps.dispatch.models import EmergencyTrip
from apps.dispatch.orchestrator import apply_new_route
from apps.fleet.models import EmergencyVehicle
from apps.network.models import Intersection, RoadEvent, RoadSegment


def build_grid(size: int = 5, spacing_deg: float = 0.004, signalise_every: int = 2):
    """A small regular grid, enough to reason about optimal paths by hand."""
    nodes = {}
    for i in range(size):
        for j in range(size):
            nodes[(i, j)] = Intersection.objects.create(
                latitude=13.0 + i * spacing_deg,
                longitude=80.0 + j * spacing_deg,
                name=f"N{i}{j}",
                is_signalised=(i + j) % signalise_every == 0,
                base_delay_s=20.0 if (i + j) % signalise_every == 0 else 2.0,
            )
    for (i, j), node in nodes.items():
        for di, dj in ((0, 1), (1, 0)):
            other = nodes.get((i + di, j + dj))
            if other is None:
                continue
            for a, b in ((node, other), (other, node)):
                RoadSegment.objects.create(
                    from_node=a, to_node=b, name=f"{a.name}-{b.name}",
                    road_class=RoadClass.SECONDARY, length_m=440.0, lanes=2,
                    free_flow_kmh=40.0,
                    geometry=[[a.latitude, a.longitude], [b.latitude, b.longitude]],
                    latitude=(a.latitude + b.latitude) / 2,
                    longitude=(a.longitude + b.longitude) / 2,
                )
    graph_mod.invalidate()
    return nodes


class RoutingTests(TestCase):
    def setUp(self):
        self.nodes = build_grid()
        self.origin = Point(self.nodes[(0, 0)].latitude, self.nodes[(0, 0)].longitude)
        self.destination = Point(self.nodes[(4, 4)].latitude, self.nodes[(4, 4)].longitude)

    def tearDown(self):
        graph_mod.invalidate()

    def test_astar_matches_dijkstra_optimum(self):
        """The A* heuristic must be admissible, or it is not really A*."""
        astar = route_between(self.origin, self.destination, priority_level=1, algorithm="astar")
        dijkstra = route_between(self.origin, self.destination, priority_level=1, algorithm="dijkstra")
        self.assertAlmostEqual(astar.total_duration_s, dijkstra.total_duration_s, delta=0.5)

    def test_astar_expands_no_more_nodes_than_dijkstra(self):
        astar = route_between(self.origin, self.destination, priority_level=1, algorithm="astar")
        dijkstra = route_between(self.origin, self.destination, priority_level=1, algorithm="dijkstra")
        self.assertLessEqual(astar.nodes_expanded, dijkstra.nodes_expanded)

    def test_route_is_connected_end_to_end(self):
        route = route_between(self.origin, self.destination, priority_level=1)
        for step, next_step in zip(route.steps, route.steps[1:]):
            self.assertEqual(step.to_node, next_step.from_node)
        self.assertGreater(len(route.geometry), 2)

    def test_arrival_offsets_increase_monotonically(self):
        route = route_between(self.origin, self.destination, priority_level=1)
        offsets = [s.exit_offset_s for s in route.steps]
        self.assertEqual(offsets, sorted(offsets))
        self.assertAlmostEqual(route.steps[0].enter_offset_s, 0.0, places=6)

    def test_priority_level_changes_cost_of_signals(self):
        """A Level 1 vehicle pays almost nothing at signals; Level 4 pays fully."""
        critical = route_between(self.origin, self.destination, priority_level=PriorityLevel.CRITICAL)
        transport = route_between(
            self.origin, self.destination, priority_level=PriorityLevel.NON_CRITICAL
        )
        self.assertLess(critical.total_duration_s, transport.total_duration_s)

    def test_position_at_interpolates_along_route(self):
        route = route_between(self.origin, self.destination, priority_level=1)
        start = route.position_at(0)
        middle = route.position_at(route.total_duration_s / 2)
        end = route.position_at(route.total_duration_s * 2)
        self.assertAlmostEqual(start.lat, self.origin.lat, places=4)
        self.assertAlmostEqual(end.lat, self.destination.lat, places=4)
        self.assertNotAlmostEqual(middle.lat, start.lat, places=4)

    def test_blocked_segment_is_avoided(self):
        route = route_between(self.origin, self.destination, priority_level=1)
        blocked_id = route.steps[1].segment_id
        RoadSegment.objects.filter(id=blocked_id).update(is_open=False)
        graph_mod.invalidate()

        rerouted = route_between(self.origin, self.destination, priority_level=1)
        self.assertNotIn(blocked_id, [s.segment_id for s in rerouted.steps])

    def test_closure_event_removes_the_link(self):
        route = route_between(self.origin, self.destination, priority_level=1)
        segment = RoadSegment.objects.get(id=route.steps[1].segment_id)
        RoadEvent.objects.create(
            event_type="closure", segment=segment,
            latitude=segment.latitude, longitude=segment.longitude,
            severity=1.0, confidence=1.0, source="operator",
        )
        rerouted = route_between(self.origin, self.destination, priority_level=1)
        self.assertNotIn(segment.id, [s.segment_id for s in rerouted.steps])

    def test_unreachable_destination_raises(self):
        RoadSegment.objects.all().delete()
        graph_mod.invalidate()
        with self.assertRaises(RouteNotFound):
            route_between(self.origin, self.destination, priority_level=1)


class CongestionTests(TestCase):
    def setUp(self):
        build_grid(size=3)

    def tearDown(self):
        graph_mod.invalidate()

    def test_default_curve_dips_at_rush_hour(self):
        morning_peak = timezone.localtime().replace(hour=9, minute=15)
        pre_dawn = timezone.localtime().replace(hour=4, minute=0)
        # A weekday, so the peak is not damped.
        while morning_peak.weekday() >= 5:
            morning_peak += timezone.timedelta(days=1)
            pre_dawn += timezone.timedelta(days=1)
        self.assertLess(default_speed_factor(morning_peak), default_speed_factor(pre_dawn))

    def test_live_reading_dominates_at_zero_horizon(self):
        segment = RoadSegment.objects.first()
        segment.apply_speed(10.0)  # heavily congested right now
        graph_mod.invalidate()
        edge = graph_mod.get_state(force=True).edges[segment.id]

        forecaster = build_forecaster()
        now = forecaster.forecast(edge, 0)
        self.assertIn(now.basis, {"live", "blended"})
        self.assertLess(now.speed_kmh, segment.design_speed_kmh)

    def test_confidence_in_live_reading_decays_with_horizon(self):
        segment = RoadSegment.objects.first()
        segment.apply_speed(5.0)
        graph_mod.invalidate()
        edge = graph_mod.get_state(force=True).edges[segment.id]

        forecaster = build_forecaster()
        near = forecaster.forecast(edge, 0).speed_kmh
        far = forecaster.forecast(edge, 3600).speed_kmh
        # A jam observed now should not be assumed to persist an hour later.
        self.assertGreater(far, near)

    def test_event_penalty_slows_the_forecast(self):
        segment = RoadSegment.objects.first()
        edge = graph_mod.get_state(force=True).edges[segment.id]

        clean = CongestionForecaster().forecast(edge, 0).speed_kmh
        penalised = CongestionForecaster(event_penalty={segment.id: 0.4}).forecast(edge, 0).speed_kmh
        self.assertLess(penalised, clean)


class CongestionRerouteTests(TestCase):
    """Layer 2 - heavy congestion ahead must move the vehicle off the jam.

    The grid gives every origin/destination pair at least two roughly equal
    paths, so jamming one of them leaves a genuinely faster alternative and the
    decision is not a coin toss.
    """

    def setUp(self):
        from apps.hospitals.rules import seed_rules

        seed_rules()
        self.nodes = build_grid(size=5)
        self.origin = self.nodes[(0, 0)]
        self.destination = self.nodes[(4, 4)]

        self.vehicle = EmergencyVehicle.objects.create(
            callsign="CONG-01",
            latitude=self.origin.latitude,
            longitude=self.origin.longitude,
            status=VehicleStatus.TRANSPORTING,
            priority_level=PriorityLevel.CRITICAL,
        )
        self.trip = EmergencyTrip.objects.create(
            vehicle=self.vehicle,
            emergency_category="cardiac",
            stage=TripStage.TO_HOSPITAL,
            priority_level=PriorityLevel.CRITICAL,
            destination_latitude=self.destination.latitude,
            destination_longitude=self.destination.longitude,
        )
        route = route_between(
            Point(self.origin.latitude, self.origin.longitude),
            Point(self.destination.latitude, self.destination.longitude),
            priority_level=PriorityLevel.CRITICAL,
        )
        self.plan = apply_new_route(self.trip, route, reason="initial")
        self.trip.refresh_from_db()

    def tearDown(self):
        graph_mod.invalidate()

    def _jam_route_ahead(self, speed_kmh: float = 4.0):
        """Bring every segment on the planned route to a crawl."""
        segment_ids = [step["segment_id"] for step in self.plan.steps]
        for segment in RoadSegment.objects.filter(id__in=segment_ids):
            segment.apply_speed(speed_kmh)
        graph_mod.invalidate()
        return segment_ids

    def test_a_clear_route_is_not_congested(self):
        decision = evaluate_trip(self.trip, force=True)
        self.assertFalse(decision.congested)

    def test_jammed_segments_are_reported_as_congested(self):
        self._jam_route_ahead()
        decision = evaluate_trip(self.trip)
        self.assertTrue(
            decision.congested,
            "a route whose every segment is crawling must be flagged congested",
        )

    def test_congestion_threshold_matches_the_heavy_band(self):
        """0.55 is where CongestionLevel stops saying 'moderate'.

        Guards the invariant the map depends on: the colour, the popup word
        and the reroute trigger must all change at the same point.
        """
        segment = RoadSegment.objects.first()

        # Just below ratio 0.45 is the first "heavy" reading (the boundary
        # itself belongs to moderate).
        segment.apply_speed(segment.design_speed_kmh * 0.40)
        segment.refresh_from_db()
        self.assertEqual(segment.congestion_level, CongestionLevel.HEAVY)
        self.assertIn(segment.id, congested_segment_ids())

        # Exactly on the boundary: still moderate, so still not a trigger.
        segment.apply_speed(segment.design_speed_kmh * 0.45)
        segment.refresh_from_db()
        self.assertEqual(segment.congestion_level, CongestionLevel.MODERATE)
        self.assertNotIn(segment.id, congested_segment_ids())

    def test_congestion_bypasses_the_replan_cooldown(self):
        """The cooldown is why a vehicle used to drive into a known jam.

        The plan was computed moments ago, so an ordinary evaluation returns
        'cooldown active'. Congestion must not be silenced by it.
        """
        clear = evaluate_trip(self.trip)
        self.assertEqual(clear.reason, "replan cooldown active")

        self._jam_route_ahead()
        congested = evaluate_trip(self.trip)
        self.assertNotEqual(congested.reason, "replan cooldown active")

    def test_vehicle_is_moved_onto_the_faster_road(self):
        """End to end: jam the route, and the trip ends up on a new one."""
        jammed = set(self._jam_route_ahead())

        decision = evaluate_trip(self.trip)
        self.assertTrue(decision.should_reroute, decision.reason)
        self.assertTrue(decision.congested)
        self.assertIn("heavy congestion", decision.reason)

        apply_new_route(self.trip, decision.route, reason=decision.reason)
        self.trip.refresh_from_db()

        new_segments = {step["segment_id"] for step in self.trip.active_route.steps}
        self.assertTrue(
            new_segments - jammed,
            "the replanned route must use at least one road that is not jammed",
        )

    def test_a_slower_detour_is_still_refused(self):
        """Congestion lowers the bar; it does not remove it.

        The road ahead is heavy, but every way around it is worse - so the
        vehicle stays put rather than abandoning an armed green corridor to
        arrive later.
        """
        on_route = set(self._jam_route_ahead(speed_kmh=15.0))
        for segment in RoadSegment.objects.exclude(id__in=on_route):
            segment.apply_speed(2.0)
        graph_mod.invalidate()

        decision = evaluate_trip(self.trip)
        self.assertTrue(decision.congested)
        self.assertFalse(decision.should_reroute, decision.reason)
        # Zero, not negative: the best alternative the router can find *is*
        # the current route. Both sides are priced with the same model, so a
        # route compared against itself must come out even - a non-zero gain
        # here would mean the comparison is measuring the model, not the road.
        self.assertEqual(round(decision.gain_s), 0)

