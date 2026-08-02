"""Geospatial primitive tests.

These functions sit under every layer - a wrong bearing or a wrong projection
would quietly corrupt corridor timing and alert targeting everywhere else, so
they are tested against known values rather than against themselves.
"""
import math

from django.test import SimpleTestCase

from apps.core.geo import (
    Point,
    bearing_deg,
    bearing_delta,
    bounding_box,
    destination_point,
    distance_to_polyline_m,
    geohash,
    geohash_neighbours,
    haversine_m,
    point_along_polyline,
    project_on_segment,
)

CHENNAI = Point(13.0604, 80.2496)
BENGALURU = Point(12.9716, 77.5946)


class HaversineTests(SimpleTestCase):
    def test_known_distance(self):
        # Chennai to Bengaluru is ~290 km great-circle.
        distance = haversine_m(CHENNAI.lat, CHENNAI.lon, BENGALURU.lat, BENGALURU.lon)
        self.assertAlmostEqual(distance / 1000, 290, delta=5)

    def test_zero_distance(self):
        self.assertEqual(haversine_m(13.0, 80.0, 13.0, 80.0), 0.0)

    def test_symmetry(self):
        forward = haversine_m(13.0, 80.0, 13.1, 80.1)
        backward = haversine_m(13.1, 80.1, 13.0, 80.0)
        self.assertAlmostEqual(forward, backward, places=6)


class BearingTests(SimpleTestCase):
    def test_due_north(self):
        self.assertAlmostEqual(bearing_deg(13.0, 80.0, 13.1, 80.0), 0.0, places=3)

    def test_due_east(self):
        self.assertAlmostEqual(bearing_deg(13.0, 80.0, 13.0, 80.1), 90.0, delta=0.1)

    def test_delta_wraps_around_north(self):
        self.assertAlmostEqual(bearing_delta(350.0, 10.0), 20.0, places=6)
        self.assertAlmostEqual(bearing_delta(10.0, 350.0), 20.0, places=6)


class ProjectionTests(SimpleTestCase):
    def test_destination_point_round_trips(self):
        start = Point(13.0, 80.0)
        moved = destination_point(start.lat, start.lon, 45.0, 1000.0)
        self.assertAlmostEqual(haversine_m(start.lat, start.lon, moved.lat, moved.lon), 1000.0, delta=1.0)
        self.assertAlmostEqual(bearing_deg(start.lat, start.lon, moved.lat, moved.lon), 45.0, delta=0.1)

    def test_bounding_box_contains_circle(self):
        min_lat, min_lon, max_lat, max_lon = bounding_box(13.0, 80.0, 1000.0)
        for bearing in range(0, 360, 15):
            edge = destination_point(13.0, 80.0, bearing, 1000.0)
            self.assertTrue(min_lat <= edge.lat <= max_lat, f"lat out of box at {bearing}")
            self.assertTrue(min_lon <= edge.lon <= max_lon, f"lon out of box at {bearing}")

    def test_project_on_segment_midpoint(self):
        a, b = Point(13.0, 80.0), Point(13.0, 80.01)
        # A point directly north of the segment's midpoint.
        p = Point(13.001, 80.005)
        closest, distance, fraction = project_on_segment(p, a, b)
        self.assertAlmostEqual(fraction, 0.5, delta=0.02)
        self.assertAlmostEqual(distance, haversine_m(13.0, 80.005, 13.001, 80.005), delta=2.0)

    def test_project_clamps_beyond_endpoints(self):
        a, b = Point(13.0, 80.0), Point(13.0, 80.01)
        _, _, fraction = project_on_segment(Point(13.0, 79.99), a, b)
        self.assertEqual(fraction, 0.0)
        _, _, fraction = project_on_segment(Point(13.0, 80.02), a, b)
        self.assertEqual(fraction, 1.0)


class PolylineTests(SimpleTestCase):
    def setUp(self):
        self.line = [Point(13.0, 80.0), Point(13.0, 80.01), Point(13.01, 80.01)]

    def test_point_at_start_and_end(self):
        self.assertEqual(point_along_polyline(self.line, 0).as_tuple(), self.line[0].as_tuple())
        self.assertEqual(point_along_polyline(self.line, 1e9).as_tuple(), self.line[-1].as_tuple())

    def test_walking_the_line_is_monotonic(self):
        previous = 0.0
        for metres in (0, 200, 500, 900, 1500):
            here = point_along_polyline(self.line, metres)
            travelled = haversine_m(self.line[0].lat, self.line[0].lon, here.lat, here.lon)
            self.assertGreaterEqual(travelled + 1e-6, previous)
            previous = travelled

    def test_distance_to_polyline_reports_progress(self):
        target = point_along_polyline(self.line, 600.0)
        offset, along = distance_to_polyline_m(target, self.line)
        self.assertLess(offset, 1.0)
        self.assertAlmostEqual(along, 600.0, delta=5.0)


class GeohashTests(SimpleTestCase):
    def test_known_encoding_length(self):
        self.assertEqual(len(geohash(13.0604, 80.2496, 6)), 6)

    def test_nearby_points_share_prefix(self):
        a = geohash(13.0604, 80.2496, 5)
        b = geohash(13.0605, 80.2497, 5)
        self.assertEqual(a, b)

    def test_distant_points_differ(self):
        self.assertNotEqual(geohash(13.06, 80.25, 6), geohash(12.97, 77.59, 6))

    def test_neighbours_include_self_and_surround(self):
        cells = geohash_neighbours(13.0604, 80.2496, 6)
        self.assertIn(geohash(13.0604, 80.2496, 6), cells)
        # A 3x3 block, though duplicates collapse near cell corners.
        self.assertGreaterEqual(len(cells), 4)
        self.assertLessEqual(len(cells), 9)
