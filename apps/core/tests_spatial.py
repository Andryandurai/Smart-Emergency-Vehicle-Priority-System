"""Phase 3 tests: spatial backend, reference concurrency, JSON portability.

Most of these run on whichever backend is configured. The PostGIS-specific
ones skip on SQLite rather than being silently absent, so a CI run against
PostgreSQL reports genuinely broader coverage instead of the same number.
"""
from __future__ import annotations

import json
import threading
import unittest

from django.db import connection
from django.test import TestCase, TransactionTestCase

from apps.core.geo import destination_point, haversine_m
from apps.core.spatial import (
    LINESTRING_TABLES,
    POINT_TABLES,
    backend_name,
    drop_generated_column_sql,
    generated_column_sql,
    linestring_column_sql,
    postgis_available,
    reset_backend_cache,
)
from apps.hospitals.models import Hospital

CENTRE = (13.0604, 80.2496)
needs_postgis = unittest.skipUnless(
    connection.vendor == "postgresql", "requires a PostgreSQL/PostGIS connection"
)


def seed_ring(count: int = 12, radius_m: float = 5_000.0):
    """Hospitals on a ring of known radius, plus one at the centre.

    A ring makes the radius boundary exact and independent of any fixture
    guesswork: every ring member is `radius_m` away by construction.
    """
    Hospital.objects.create(
        code="CENTRE", name="Centre", latitude=CENTRE[0], longitude=CENTRE[1]
    )
    for i in range(count):
        point = destination_point(CENTRE[0], CENTRE[1], i * (360 / count), radius_m)
        Hospital.objects.create(
            code=f"RING{i:02d}", name=f"Ring {i}", latitude=point.lat, longitude=point.lon
        )


class SpatialBackendTests(TestCase):
    def setUp(self):
        reset_backend_cache()
        seed_ring()

    def test_backend_is_reported_honestly(self):
        name = backend_name()
        self.assertIn("postgis" if postgis_available() else connection.vendor, name)

    def test_radius_excludes_points_outside(self):
        found = Hospital.objects.all().near(*CENTRE, 1_000)
        self.assertEqual([h.code for h in found], ["CENTRE"])

    def test_radius_includes_the_whole_ring(self):
        found = Hospital.objects.all().near(*CENTRE, 6_000)
        self.assertEqual(len(found), 13)

    def test_results_are_ordered_nearest_first(self):
        found = Hospital.objects.all().near(*CENTRE, 20_000)
        distances = [h.distance_m for h in found]
        self.assertEqual(distances, sorted(distances))
        self.assertEqual(found[0].code, "CENTRE")

    def test_distance_is_annotated_and_accurate(self):
        found = Hospital.objects.all().near(*CENTRE, 6_000)
        ring = [h for h in found if h.code != "CENTRE"]
        for hospital in ring:
            self.assertAlmostEqual(hospital.distance_m, 5_000, delta=5.0)

    def test_near_returns_a_list_not_a_queryset(self):
        """Six call sites index and slice the result; the contract is a list."""
        found = Hospital.objects.all().near(*CENTRE, 6_000)
        self.assertIsInstance(found, list)
        self.assertTrue(hasattr(found[0], "distance_m"))

    def test_caller_filters_are_respected(self):
        """A radius search must not widen the caller's queryset."""
        Hospital.objects.filter(code="CENTRE").update(is_active=False)
        found = Hospital.objects.filter(is_active=True).near(*CENTRE, 6_000)
        self.assertNotIn("CENTRE", [h.code for h in found])
        self.assertEqual(len(found), 12)

    def test_empty_result_is_an_empty_list(self):
        self.assertEqual(Hospital.objects.all().near(-40.0, 170.0, 1_000), [])

    def test_matches_a_brute_force_haversine_scan(self):
        """The indexed path must agree with the definition of the answer."""
        radius = 4_800.0
        expected = sorted(
            h.code
            for h in Hospital.objects.all()
            if haversine_m(*CENTRE, h.latitude, h.longitude) <= radius
        )
        actual = sorted(h.code for h in Hospital.objects.all().near(*CENTRE, radius))
        self.assertEqual(actual, expected)


class SpatialSQLTests(TestCase):
    """The DDL is asserted directly so it is reviewable without a server."""

    def test_point_column_is_generated_from_the_float_columns(self):
        sql = generated_column_sql("hospitals_hospital")
        self.assertIn("geography(Point, 4326)", sql)
        self.assertIn("GENERATED ALWAYS AS", sql)
        self.assertIn("STORED", sql)
        self.assertIn('ST_MakePoint("longitude", "latitude")', sql)
        self.assertIn("USING GIST", sql)

    def test_ddl_is_idempotent(self):
        sql = generated_column_sql("hospitals_hospital")
        self.assertIn("ADD COLUMN IF NOT EXISTS", sql)
        self.assertIn("CREATE INDEX IF NOT EXISTS", sql)

    def test_down_migration_drops_index_before_column(self):
        sql = drop_generated_column_sql("hospitals_hospital")
        self.assertLess(sql.index("DROP INDEX"), sql.index("DROP COLUMN"))

    def test_linestring_column_is_not_generated(self):
        """The JSON is [lat, lon]; PostGIS wants (lon, lat) - needs a backfill."""
        sql = linestring_column_sql("network_roadsegment")
        self.assertIn("geography(LineString, 4326)", sql)
        self.assertNotIn("GENERATED", sql)

    def test_every_geo_model_is_registered(self):
        """A new model with coordinates must not silently miss its index."""
        from django.apps import apps as app_registry

        registered = {f"{a}.{m}" for a, m in POINT_TABLES}
        discovered = set()
        for model in app_registry.get_models():
            if not model._meta.app_label.startswith(("fleet", "network", "hospitals",
                                                     "alerts", "analytics", "dispatch")):
                continue
            names = {f.name for f in model._meta.get_fields()}
            if {"latitude", "longitude"} <= names:
                discovered.add(f"{model._meta.app_label}.{model.__name__}")

        self.assertEqual(
            discovered - registered,
            set(),
            "Models with coordinates missing from POINT_TABLES - they would get "
            "no spatial index on PostGIS.",
        )

    def test_linestring_registry_points_at_real_json_fields(self):
        from django.apps import apps as app_registry

        for app_label, model_name, field_name in LINESTRING_TABLES:
            model = app_registry.get_model(app_label, model_name)
            self.assertIn(field_name, {f.name for f in model._meta.get_fields()})


@needs_postgis
class PostGISIntegrityTests(TestCase):
    """Only meaningful against a real PostGIS server."""

    def setUp(self):
        reset_backend_cache()
        seed_ring()

    def test_generated_column_agrees_with_the_float_columns(self):
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT COUNT(*) FROM "hospitals_hospital" '
                'WHERE ABS(ST_Y("geom"::geometry) - "latitude") > 1e-9 '
                '   OR ABS(ST_X("geom"::geometry) - "longitude") > 1e-9'
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_generated_column_follows_an_update(self):
        """A generated column cannot drift - that is the point of choosing one."""
        hospital = Hospital.objects.get(code="CENTRE")
        hospital.latitude += 0.01
        hospital.save(update_fields=["latitude"])

        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT ST_Y("geom"::geometry) FROM "hospitals_hospital" WHERE id = %s',
                [hospital.pk],
            )
            self.assertAlmostEqual(cursor.fetchone()[0], hospital.latitude, places=9)

    def test_radius_search_uses_the_gist_index(self):
        with connection.cursor() as cursor:
            cursor.execute(
                'EXPLAIN SELECT id FROM "hospitals_hospital" '
                "WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s)",
                [CENTRE[1], CENTRE[0], 6000],
            )
            plan = " ".join(row[0] for row in cursor.fetchall())
        self.assertIn("Index", plan, f"expected an index scan, got:\n{plan}")


class TripReferenceConcurrencyTests(TransactionTestCase):
    """The read-then-write race identified in the Phase 1 analysis.

    ``TransactionTestCase`` because the threads need real, committed
    transactions - the wrapping transaction of a normal ``TestCase`` would
    hide the very interleaving under test.
    """

    def setUp(self):
        from apps.fleet.models import EmergencyVehicle

        self.vehicles = [
            EmergencyVehicle.objects.create(
                callsign=f"RACE-{i}", latitude=13.0, longitude=80.0
            )
            for i in range(12)
        ]

    def test_references_are_sequential_when_uncontended(self):
        from apps.dispatch.models import EmergencyTrip

        first = EmergencyTrip.objects.create(vehicle=self.vehicles[0])
        second = EmergencyTrip.objects.create(vehicle=self.vehicles[1])
        self.assertTrue(first.reference.startswith("SEV-"))
        self.assertEqual(int(second.reference[-4:]), int(first.reference[-4:]) + 1)

    def test_concurrent_creation_yields_no_duplicates(self):
        """Regression: this raised IntegrityError under multi-worker Postgres."""
        from django.db import connections

        from apps.dispatch.models import EmergencyTrip

        errors: list[Exception] = []
        barrier = threading.Barrier(len(self.vehicles))

        def create(vehicle):
            try:
                barrier.wait(timeout=10)       # maximise the interleaving
                EmergencyTrip.objects.create(vehicle=vehicle)
            except Exception as exc:           # noqa: BLE001 - recorded and re-raised
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=create, args=(v,)) for v in self.vehicles]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [], f"concurrent creation failed: {errors[:2]}")

        references = list(EmergencyTrip.objects.values_list("reference", flat=True))
        self.assertEqual(len(references), len(self.vehicles))
        self.assertEqual(
            len(set(references)), len(references), "duplicate trip references issued"
        )

    def test_explicit_reference_is_not_overwritten(self):
        from apps.dispatch.models import EmergencyTrip

        trip = EmergencyTrip.objects.create(
            vehicle=self.vehicles[0], reference="MANUAL-0001"
        )
        self.assertEqual(trip.reference, "MANUAL-0001")

    def test_a_malformed_existing_reference_does_not_wedge_allocation(self):
        """Hand-edited data must not stop the control room opening a call-out."""
        from django.utils import timezone

        from apps.dispatch.models import EmergencyTrip

        prefix = f"SEV-{timezone.localdate():%Y%m%d}-"
        EmergencyTrip.objects.create(vehicle=self.vehicles[0], reference=f"{prefix}oops")

        trip = EmergencyTrip.objects.create(vehicle=self.vehicles[1])
        self.assertTrue(trip.reference.startswith(prefix))


class JSONFieldPortabilityTests(TestCase):
    """SQLite stores JSON as text, PostgreSQL as jsonb.

    The risk flagged in Phase 1: a value that round-trips on one backend but
    not the other. These assert the shapes SEVPS actually stores - route
    steps, polylines and recommendation candidates.
    """

    def test_route_geometry_round_trips(self):
        from apps.dispatch.models import EmergencyTrip, RoutePlan
        from apps.fleet.models import EmergencyVehicle

        vehicle = EmergencyVehicle.objects.create(
            callsign="JSON-1", latitude=13.0, longitude=80.0
        )
        trip = EmergencyTrip.objects.create(vehicle=vehicle)
        geometry = [[13.0604, 80.2496], [13.0610, 80.2500], [13.0621, 80.2511]]
        steps = [
            {
                "segment_id": 1, "from_node": 2, "to_node": 3, "name": "Anna Salai",
                "length_m": 412.5, "travel_time_s": 37.2, "enter_offset_s": 0.0,
                "exit_offset_s": 37.2, "predicted_speed_kmh": 39.9,
                "forecast_basis": "blended", "is_signalised_exit": True,
            }
        ]
        plan = RoutePlan.objects.create(
            trip=trip, origin_latitude=13.0, origin_longitude=80.0,
            destination_latitude=13.1, destination_longitude=80.1,
            geometry=geometry, steps=steps, node_ids=[2, 3, 4],
        )
        plan.refresh_from_db()

        self.assertEqual(plan.geometry, geometry)
        self.assertEqual(plan.steps, steps)
        self.assertIs(plan.steps[0]["is_signalised_exit"], True)
        self.assertIsInstance(plan.steps[0]["length_m"], float)
        self.assertIsInstance(plan.node_ids[0], int)

    def test_empty_and_null_json_are_distinguishable(self):
        from apps.network.models import Intersection, RoadSegment

        a = Intersection.objects.create(latitude=13.0, longitude=80.0)
        b = Intersection.objects.create(latitude=13.1, longitude=80.1)
        segment = RoadSegment.objects.create(
            from_node=a, to_node=b, length_m=100.0, latitude=13.05, longitude=80.05,
            geometry=[],
        )
        segment.refresh_from_db()
        self.assertEqual(segment.geometry, [])

    def test_nested_recommendation_payload_round_trips(self):
        """The audit log stores the full candidate list with nested factors."""
        from apps.dispatch.models import EmergencyTrip
        from apps.fleet.models import EmergencyVehicle
        from apps.hospitals.models import HospitalRecommendationLog

        vehicle = EmergencyVehicle.objects.create(
            callsign="JSON-2", latitude=13.0, longitude=80.0
        )
        trip = EmergencyTrip.objects.create(vehicle=vehicle)
        candidates = [
            {
                "code": "APOLLO", "score": 0.8754, "eligible": True,
                "factors": {"capability": 0.9, "travel_time": 0.83},
                "warnings": [], "missing_facilities": None,
                "within_golden_window": True,
            },
            {
                "code": "NEAR", "score": 0.0, "eligible": False,
                "exclusion_reason": "missing required facilities: burn_unit",
                "missing_facilities": ["burn_unit"], "within_golden_window": None,
            },
        ]
        log = HospitalRecommendationLog.objects.create(
            trip=trip, emergency_category="burn", candidates=candidates,
            rule_snapshot={"required_facilities": ["burn_unit"], "golden_window_min": 120},
        )
        log.refresh_from_db()

        self.assertEqual(log.candidates, candidates)
        self.assertIsNone(log.candidates[1]["within_golden_window"])
        self.assertEqual(log.rule_snapshot["golden_window_min"], 120)
        # Serialising the reloaded value must not raise - it is sent over the
        # API and the WebSocket verbatim.
        json.dumps(log.candidates)
