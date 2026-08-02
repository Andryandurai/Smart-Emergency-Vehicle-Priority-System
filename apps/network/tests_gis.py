"""Phase 8 tests: GIS layers, coordinate order, per-layer permissions.

The coordinate-order tests are the ones that matter most. GeoJSON is
[lon, lat] and Leaflet is [lat, lon]; getting it backwards puts a Chennai
ambulance in the Indian Ocean and the map looks empty rather than wrong, so it
is exactly the kind of bug that survives review.
"""
from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings

from apps.core.roles import Role
from apps.network import gis

CHENNAI_LAT, CHENNAI_LON = 13.0604, 80.2496


class GeoJSONShapeTests(TestCase):
    def setUp(self):
        from apps.hospitals.models import Hospital, HospitalCapability, HospitalCapacity
        from apps.network.models import Intersection, RoadSegment, TrafficSignal

        a = Intersection.objects.create(
            latitude=CHENNAI_LAT, longitude=CHENNAI_LON, name="A", is_signalised=True
        )
        b = Intersection.objects.create(latitude=13.07, longitude=80.26, name="B")
        self.segment = RoadSegment.objects.create(
            from_node=a, to_node=b, name="Anna Salai", length_m=400.0,
            latitude=13.065, longitude=80.255,
            geometry=[[CHENNAI_LAT, CHENNAI_LON], [13.07, 80.26]],
        )
        TrafficSignal.objects.create(intersection=a, controller_id="TSC-1")

        self.hospital = Hospital.objects.create(
            code="GIS", name="GIS Hospital", latitude=CHENNAI_LAT, longitude=CHENNAI_LON
        )
        HospitalCapability.objects.create(hospital=self.hospital, facility="emergency_dept")
        HospitalCapacity.objects.create(hospital=self.hospital)

    # -- coordinate order ---------------------------------------------------
    def test_points_are_lon_lat_not_lat_lon(self):
        """The bug that empties a map instead of visibly breaking it."""
        collection = gis.hospitals()
        longitude, latitude = collection["features"][0]["geometry"]["coordinates"]
        self.assertAlmostEqual(longitude, CHENNAI_LON, places=4)
        self.assertAlmostEqual(latitude, CHENNAI_LAT, places=4)
        # Chennai: longitude ~80, latitude ~13. Swapped would be off India.
        self.assertGreater(longitude, latitude)

    def test_linestrings_are_lon_lat(self):
        first = gis.road_network()["features"][0]["geometry"]["coordinates"][0]
        self.assertAlmostEqual(first[0], CHENNAI_LON, places=4)
        self.assertAlmostEqual(first[1], CHENNAI_LAT, places=4)

    def test_the_conversion_helper_flips_exactly_once(self):
        converted = gis.latlon_pairs_to_geojson([[13.0, 80.0], [13.1, 80.1]])
        self.assertEqual(converted, [[80.0, 13.0], [80.1, 13.1]])

    # -- shape --------------------------------------------------------------
    def test_every_layer_returns_a_valid_feature_collection(self):
        for name in gis.LAYERS:
            with self.subTest(layer=name):
                collection = gis.build(name)
                self.assertEqual(collection["type"], "FeatureCollection")
                self.assertIsInstance(collection["features"], list)
                self.assertIn("generated_at", collection["metadata"])
                self.assertEqual(collection["metadata"]["count"], len(collection["features"]))

    def test_every_feature_has_geometry_and_properties(self):
        for name in gis.LAYERS:
            for item in gis.build(name)["features"]:
                with self.subTest(layer=name):
                    self.assertEqual(item["type"], "Feature")
                    self.assertIn(item["geometry"]["type"], ("Point", "LineString"))
                    self.assertIsInstance(item["properties"], dict)

    def test_geometry_type_matches_the_declared_spec(self):
        for name, spec in gis.LAYERS.items():
            features = gis.build(name)["features"]
            for item in features:
                with self.subTest(layer=name):
                    self.assertEqual(item["geometry"]["type"], spec.geometry)

    # -- content ------------------------------------------------------------
    def test_hospital_properties_carry_what_a_router_needs(self):
        properties = gis.hospitals()["features"][0]["properties"]
        for key in ("code", "name", "is_on_diversion", "facilities",
                    "emergency_beds_available", "workload_index"):
            self.assertIn(key, properties)

    def test_signal_layer_reports_preemption_state(self):
        properties = gis.traffic_signals()["features"][0]["properties"]
        self.assertIn("is_preempted", properties)
        self.assertIn("supports_preemption", properties)

    def test_route_layer_carries_no_clinical_fields(self):
        """Consumed by traffic-side clients with no clearance for them."""
        from apps.core.enums import TripStage
        from apps.dispatch.models import EmergencyTrip, RoutePlan
        from apps.fleet.models import EmergencyVehicle

        vehicle = EmergencyVehicle.objects.create(
            callsign="GIS-1", latitude=CHENNAI_LAT, longitude=CHENNAI_LON
        )
        trip = EmergencyTrip.objects.create(
            vehicle=vehicle, stage=TripStage.TO_HOSPITAL,
            patient_age=61, patient_notes="Confidential",
        )
        RoutePlan.objects.create(
            trip=trip, origin_latitude=13.0, origin_longitude=80.0,
            destination_latitude=13.1, destination_longitude=80.1,
            geometry=[[13.0, 80.0], [13.05, 80.05]],
            total_distance_m=900.0, total_duration_s=120.0,
        )

        properties = gis.emergency_routes()["features"][0]["properties"]
        for leaked in ("patient_age", "patient_notes", "patient_deteriorating"):
            self.assertNotIn(leaked, properties)
        self.assertIn("reference", properties)

    def test_road_network_reports_truncation(self):
        collection = gis.road_network(limit=1)
        self.assertTrue(collection["metadata"]["truncated"])

    def test_unknown_layer_raises(self):
        with self.assertRaises(KeyError):
            gis.build("not_a_layer")


class HeatmapTests(TestCase):
    def setUp(self):
        from apps.analytics.models import Hotspot
        from apps.network.models import Intersection, RoadSegment

        a = Intersection.objects.create(latitude=13.0, longitude=80.0)
        b = Intersection.objects.create(latitude=13.01, longitude=80.01)
        segment = RoadSegment.objects.create(
            from_node=a, to_node=b, length_m=300.0, latitude=13.005, longitude=80.005
        )
        segment.apply_speed(5.0)      # heavily congested -> high weight

        Hotspot.objects.create(
            kind=Hotspot.Kind.ACCIDENT, label="Blackspot",
            latitude=13.02, longitude=80.02, incident_count=12, score=0.9,
        )

    def test_congestion_heatmap_weights_are_normalised(self):
        for item in gis.congestion_heatmap()["features"]:
            weight = item["properties"]["weight"]
            self.assertGreaterEqual(weight, 0.0)
            self.assertLessEqual(weight, 1.0)

    def test_congestion_heatmap_omits_free_flowing_roads(self):
        """A heat surface of every road is a solid rectangle, not information."""
        from apps.network.models import RoadSegment

        RoadSegment.objects.all().update(congestion_index=0.0)
        self.assertEqual(gis.congestion_heatmap()["metadata"]["count"], 0)

    def test_accident_heatmap_uses_the_clustered_hotspots(self):
        collection = gis.accident_heatmap()
        self.assertEqual(collection["metadata"]["count"], 1)
        self.assertEqual(collection["features"][0]["properties"]["incident_count"], 12)

    def test_heatmap_layers_declare_their_weight_field(self):
        for name in ("congestion_heatmap", "accident_heatmap", "delay_heatmap"):
            self.assertEqual(gis.build(name)["metadata"]["weight_field"], "weight")

    def test_delay_heatmap_is_empty_without_history(self):
        self.assertEqual(gis.delay_heatmap()["metadata"]["count"], 0)


class BasemapTests(TestCase):
    def test_osm_is_available_without_any_key(self):
        """An emergency platform must not need a tile contract to draw a map."""
        providers = gis.basemap_providers()
        keyless = [p for p in providers["providers"] if not p["requires_key"]]
        self.assertTrue(keyless)
        self.assertFalse(next(p for p in providers["providers"] if p["default"])["requires_key"])

    def test_mapbox_absent_without_a_token(self):
        providers = gis.basemap_providers()
        self.assertFalse(providers["mapbox_available"])
        self.assertFalse(any(p["id"].startswith("mapbox") for p in providers["providers"]))

    def test_mapbox_appears_with_a_token_and_includes_traffic(self):
        from django.conf import settings

        config = {**settings.SEVPS, "MAPBOX_TOKEN": "pk.test-token"}
        with override_settings(SEVPS=config):
            providers = gis.basemap_providers()
        self.assertTrue(providers["mapbox_available"])
        mapbox = [p for p in providers["providers"] if p["id"].startswith("mapbox")]
        self.assertEqual(len(mapbox), 2)
        self.assertTrue(any(p.get("is_traffic") for p in mapbox))
        self.assertIn("pk.test-token", mapbox[0]["url"])

    def test_google_maps_stays_configuration_only(self):
        from django.conf import settings

        config = {**settings.SEVPS, "GOOGLE_MAPS_KEY": "test-key"}
        with override_settings(SEVPS=config):
            providers = gis.basemap_providers()
        self.assertTrue(providers["google_maps_available"])
        # Available, but deliberately not rendered as a provider.
        self.assertFalse(any("google" in p["id"] for p in providers["providers"]))
        self.assertIn("duplicates", providers["google_maps_note"])


class LayerPermissionTests(TestCase):
    """Public infrastructure is open; live operational data is not."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("gis_user", password="pw")
        group, _ = Group.objects.get_or_create(name=Role.TRAFFIC_POLICE)
        cls.user.groups.add(group)

    def test_catalogue_is_public(self):
        response = self.client.get("/api/v1/network/gis/layers/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["layers"])

    def test_public_layers_need_no_credentials(self):
        for name in ("road_network", "hospitals", "traffic_signals", "road_closures"):
            with self.subTest(layer=name):
                response = self.client.get(f"/api/v1/network/gis/layers/{name}/")
                self.assertEqual(response.status_code, 200)

    def test_operational_layers_are_denied_anonymously(self):
        for name in ("emergency_vehicles", "emergency_routes", "accident_heatmap"):
            with self.subTest(layer=name):
                response = self.client.get(f"/api/v1/network/gis/layers/{name}/")
                self.assertIn(response.status_code, (401, 403))

    def test_operational_layers_open_for_a_signed_in_role(self):
        self.client.force_login(self.user)
        for name in ("emergency_vehicles", "emergency_routes"):
            with self.subTest(layer=name):
                self.assertEqual(
                    self.client.get(f"/api/v1/network/gis/layers/{name}/").status_code, 200
                )

    def test_unknown_layer_returns_404_and_lists_the_real_ones(self):
        response = self.client.get("/api/v1/network/gis/layers/nonsense/")
        self.assertEqual(response.status_code, 404)
        self.assertIn("road_network", response.json()["available"])

    def test_bad_query_parameter_is_rejected_not_ignored(self):
        response = self.client.get("/api/v1/network/gis/layers/road_network/?limit=lots")
        self.assertEqual(response.status_code, 400)

    def test_catalogue_declares_permissions_matching_enforcement(self):
        """A layer advertised as public must actually be reachable anonymously."""
        catalogue = self.client.get("/api/v1/network/gis/layers/").json()["layers"]
        for entry in catalogue:
            response = self.client.get(entry["url"])
            with self.subTest(layer=entry["name"]):
                if entry["public"]:
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertIn(response.status_code, (401, 403))
