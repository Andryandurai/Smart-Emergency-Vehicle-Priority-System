"""Phase 7 tests: the six detectors, and the judgement calls inside them.

Each detector is tested against hand-built frames rather than a model, because
what is being asserted is the *reasoning* - that people on a jammed road are
treated differently from people on a clear one, that a vehicle still in moving
traffic is parked while one still in a jam is not. Those distinctions are the
difference between a useful detector and an alarm nobody trusts.
"""
from __future__ import annotations

from django.test import SimpleTestCase, TestCase

from apps.network.cv import analysers, pipeline
from apps.network.cv.detection import (
    Detection,
    Finding,
    analysis_confidence,
    congestion_from_speed,
    density_from_detections,
    occupancy_from_detections,
    speed_from_density,
)


def car(x: float = 0.1, y: float = 0.5, *, motion: float | None = None, track: int | None = None):
    return Detection("car", 0.9, (x, y, x + 0.1, y + 0.06), track_id=track, motion=motion)


def person(x: float = 0.4, y: float = 0.5):
    return Detection("person", 0.8, (x, y, x + 0.04, y + 0.14), motion=0.0)


# ---------------------------------------------------------------------------
# Density and speed
# ---------------------------------------------------------------------------
class DensityTests(SimpleTestCase):
    def test_density_scales_inversely_with_lanes(self):
        vehicles = [car(x=i * 0.1) for i in range(8)]
        narrow = density_from_detections(vehicles, lanes=1)
        wide = density_from_detections(vehicles, lanes=4)
        self.assertAlmostEqual(narrow, wide * 4, places=3)

    def test_speed_falls_as_density_rises(self):
        empty = speed_from_density(0, 50)
        busy = speed_from_density(65, 50)
        jammed = speed_from_density(130, 50)
        self.assertAlmostEqual(empty, 50, places=1)
        self.assertLess(busy, empty)
        self.assertLess(jammed, busy)

    def test_speed_never_reaches_zero(self):
        """A zero speed would make the router's travel time infinite."""
        self.assertGreaterEqual(speed_from_density(500, 50), 3.0)

    def test_occupancy_is_capped_at_one(self):
        overlapping = [Detection("car", 0.9, (0, 0, 1, 1)) for _ in range(4)]
        self.assertEqual(occupancy_from_detections(overlapping), 1.0)

    def test_congestion_level_tracks_the_speed_ratio(self):
        self.assertEqual(congestion_from_speed(48, 50), "free")
        self.assertEqual(congestion_from_speed(5, 50), "jam")

    def test_empty_frame_is_low_confidence(self):
        """An empty frame is equally consistent with a clear road and a broken camera."""
        self.assertLess(analysis_confidence([], []), 0.5)

    def test_confidence_grows_with_the_sample(self):
        few = [car(x=0.1)]
        many = [car(x=i * 0.05) for i in range(15)]
        self.assertLess(analysis_confidence(few, few), analysis_confidence(many, many))


# ---------------------------------------------------------------------------
# Accident
# ---------------------------------------------------------------------------
class AccidentDetectionTests(SimpleTestCase):
    def test_people_on_a_clear_stopped_road_is_the_signature(self):
        frame = [person(), car(0.1, motion=0.0), car(0.3, motion=0.0)]
        finding = analysers.detect_accident(frame, occupancy=0.2, speed_ratio=0.05)
        self.assertIsNotNone(finding)
        self.assertGreater(finding.confidence, 0.55)
        self.assertTrue(finding.is_actionable)

    def test_the_same_people_on_a_jammed_road_score_lower(self):
        """Pedestrians beside congested traffic are common and innocent."""
        frame = [person(), car(0.1, motion=0.0), car(0.3, motion=0.0)]
        clear = analysers.detect_accident(frame, occupancy=0.2, speed_ratio=0.05)
        jammed = analysers.detect_accident(frame, occupancy=0.85, speed_ratio=0.05)
        self.assertLess(jammed.confidence, clear.confidence)

    def test_free_flowing_traffic_yields_nothing(self):
        frame = [person(), car(0.1, motion=0.5)]
        self.assertIsNone(analysers.detect_accident(frame, occupancy=0.2, speed_ratio=0.9))

    def test_no_people_yields_nothing(self):
        frame = [car(0.1, motion=0.0), car(0.3, motion=0.0)]
        self.assertIsNone(analysers.detect_accident(frame, occupancy=0.2, speed_ratio=0.05))

    def test_confidence_is_capped_below_certainty(self):
        """This dispatches units; it should prompt a human, not act alone."""
        frame = [person(), person(0.5), car(0.1, motion=0.0), car(0.3, motion=0.0)]
        finding = analysers.detect_accident(frame, occupancy=0.1, speed_ratio=0.0)
        self.assertLessEqual(finding.confidence, 0.85)

    def test_evidence_is_always_recorded(self):
        frame = [person(), car(0.1, motion=0.0), car(0.3, motion=0.0)]
        finding = analysers.detect_accident(frame, occupancy=0.2, speed_ratio=0.05)
        self.assertTrue(finding.evidence)
        self.assertTrue(all(isinstance(line, str) for line in finding.evidence))


# ---------------------------------------------------------------------------
# Road block
# ---------------------------------------------------------------------------
class RoadBlockTests(SimpleTestCase):
    def test_full_stationary_carriageway_is_a_block(self):
        frame = [car(x=i * 0.1, motion=0.0) for i in range(8)]
        finding = analysers.detect_road_block(
            frame, occupancy=0.85, speed_ratio=0.05, lanes=3
        )
        self.assertIsNotNone(finding)
        self.assertTrue(finding.is_actionable)

    def test_moving_traffic_is_not_a_block_however_dense(self):
        """A jam is not a blockage; the router should cost it, not avoid it."""
        frame = [car(x=i * 0.1, motion=0.4) for i in range(8)]
        self.assertIsNone(
            analysers.detect_road_block(frame, occupancy=0.9, speed_ratio=0.5, lanes=3)
        )

    def test_people_in_the_carriageway_raise_confidence(self):
        still = [car(x=i * 0.1, motion=0.0) for i in range(8)]
        without = analysers.detect_road_block(still, occupancy=0.85, speed_ratio=0.05, lanes=3)
        with_people = analysers.detect_road_block(
            still + [person()], occupancy=0.85, speed_ratio=0.05, lanes=3
        )
        self.assertGreater(with_people.confidence, without.confidence)

    def test_an_empty_road_is_not_a_block(self):
        self.assertIsNone(
            analysers.detect_road_block([], occupancy=0.05, speed_ratio=0.95, lanes=2)
        )


# ---------------------------------------------------------------------------
# Illegal parking
# ---------------------------------------------------------------------------
class IllegalParkingTests(SimpleTestCase):
    def test_still_vehicle_while_traffic_flows_is_parked(self):
        frame = [car(track=7, motion=0.0)]
        finding = analysers.detect_illegal_parking(
            frame, still_tracks={7: 4}, speed_ratio=0.8
        )
        self.assertIsNotNone(finding)
        self.assertTrue(finding.is_actionable)

    def test_still_vehicle_in_a_jam_is_not_parked(self):
        """The hard case: at a red light every vehicle looks parked."""
        frame = [car(track=7, motion=0.0)]
        self.assertIsNone(
            analysers.detect_illegal_parking(frame, still_tracks={7: 4}, speed_ratio=0.1)
        )

    def test_brief_stillness_is_not_enough(self):
        frame = [car(track=7, motion=0.0)]
        self.assertIsNone(
            analysers.detect_illegal_parking(frame, still_tracks={7: 1}, speed_ratio=0.8)
        )

    def test_severity_stays_minor(self):
        """It narrows the road; it does not close it."""
        frame = [car(track=i, motion=0.0) for i in range(4)]
        finding = analysers.detect_illegal_parking(
            frame, still_tracks={i: 5 for i in range(4)}, speed_ratio=0.8
        )
        self.assertLessEqual(finding.severity, 0.4)


# ---------------------------------------------------------------------------
# Emergency vehicle
# ---------------------------------------------------------------------------
class EmergencyVehicleTests(SimpleTestCase):
    FLEET = [{"callsign": "AMB-101", "priority_level": 1, "distance_m": 40}]

    def test_telemetry_plus_a_visible_large_vehicle_is_high_confidence(self):
        frame = [Detection("truck", 0.85, (0.3, 0.4, 0.5, 0.7))]
        sightings, findings = analysers.detect_emergency_vehicles(frame, nearby_fleet=self.FLEET)
        self.assertEqual(len(sightings), 1)
        self.assertTrue(sightings[0]["visually_corroborated"])
        self.assertGreater(sightings[0]["confidence"], 0.8)
        self.assertTrue(findings[0].evidence)

    def test_an_empty_frame_halves_confidence(self):
        """Telemetry says it is here; the camera cannot see it. Say so."""
        sightings, _ = analysers.detect_emergency_vehicles([], nearby_fleet=self.FLEET)
        self.assertLess(sightings[0]["confidence"], 0.6)
        self.assertFalse(sightings[0]["visually_corroborated"])

    def test_no_fleet_nearby_means_no_sighting(self):
        frame = [Detection("truck", 0.9, (0.3, 0.4, 0.5, 0.7))]
        sightings, findings = analysers.detect_emergency_vehicles(frame, nearby_fleet=[])
        self.assertEqual(sightings, [])
        self.assertEqual(findings, [])

    def test_distance_erodes_confidence(self):
        near, _ = analysers.detect_emergency_vehicles(
            [], nearby_fleet=[{"callsign": "A", "priority_level": 1, "distance_m": 20}]
        )
        far, _ = analysers.detect_emergency_vehicles(
            [], nearby_fleet=[{"callsign": "A", "priority_level": 1, "distance_m": 220}]
        )
        self.assertGreater(near[0]["confidence"], far[0]["confidence"])

    def test_sighting_is_informational_not_a_road_problem(self):
        frame = [Detection("bus", 0.9, (0.3, 0.4, 0.5, 0.7))]
        _, findings = analysers.detect_emergency_vehicles(frame, nearby_fleet=self.FLEET)
        self.assertEqual(findings[0].severity, 0.0)


# ---------------------------------------------------------------------------
# Findings and the pipeline
# ---------------------------------------------------------------------------
class FindingTests(SimpleTestCase):
    def test_actionability_threshold(self):
        self.assertTrue(Finding("accident", 0.6, 0.7, "x").is_actionable)
        self.assertFalse(Finding("accident", 0.4, 0.7, "x").is_actionable)


class PipelineTests(TestCase):
    def setUp(self):
        from apps.network.models import CameraFeed, Intersection, RoadSegment

        pipeline.reset_tracking()
        a = Intersection.objects.create(latitude=13.0, longitude=80.0)
        b = Intersection.objects.create(latitude=13.01, longitude=80.01)
        self.segment = RoadSegment.objects.create(
            from_node=a, to_node=b, length_m=400.0, lanes=3, free_flow_kmh=50.0,
            latitude=13.005, longitude=80.005,
        )
        self.camera = CameraFeed.objects.create(
            name="CV-TEST", segment=self.segment, intersection=b,
            latitude=13.005, longitude=80.005,
        )

    def test_analysis_produces_a_complete_picture(self):
        analysis = pipeline.analyse_camera(self.camera)
        payload = analysis.as_dict()
        for key in ("vehicle_count", "density", "occupancy", "estimated_speed_kmh",
                    "congestion_level", "confidence", "backend", "findings"):
            self.assertIn(key, payload)

    def test_ingest_writes_an_observation_and_updates_the_segment(self):
        from apps.network.models import TrafficObservation

        analysis = pipeline.analyse_camera(self.camera)
        observation, _ = pipeline.ingest(self.camera, analysis)

        self.assertIsNotNone(observation)
        self.assertEqual(TrafficObservation.objects.count(), 1)
        self.segment.refresh_from_db()
        self.assertIsNotNone(self.segment.current_speed_kmh)
        self.camera.refresh_from_db()
        self.assertIsNotNone(self.camera.last_analysed_at)

    def test_low_confidence_findings_do_not_create_road_events(self):
        """A guess must not reroute ambulances off a possibly-clear road."""
        from apps.network.cv.detection import FrameAnalysis
        from apps.network.models import RoadEvent

        analysis = FrameAnalysis(
            vehicle_count=2, density=5.0, occupancy=0.2, estimated_speed_kmh=30.0,
            congestion_level="light",
            findings=[Finding("accident", confidence=0.30, severity=0.8, summary="maybe")],
        )
        _, created = pipeline.ingest(self.camera, analysis)
        self.assertEqual(created, [])
        self.assertEqual(RoadEvent.objects.count(), 0)

    def test_confident_findings_do_create_road_events(self):
        from apps.network.cv.detection import FrameAnalysis
        from apps.network.models import RoadEvent

        analysis = FrameAnalysis(
            vehicle_count=9, density=60.0, occupancy=0.85, estimated_speed_kmh=4.0,
            congestion_level="jam",
            findings=[
                Finding("accident", confidence=0.75, severity=0.8, summary="collision",
                        evidence=["people in carriageway"])
            ],
        )
        _, created = pipeline.ingest(self.camera, analysis)
        self.assertEqual(len(created), 1)
        self.assertEqual(RoadEvent.objects.count(), 1)
        self.assertIn("people in carriageway", RoadEvent.objects.first().description)

    def test_repeat_findings_update_rather_than_duplicate(self):
        """A camera reporting the same jam every sweep must not spam events."""
        from apps.network.cv.detection import FrameAnalysis
        from apps.network.models import RoadEvent

        def analysis(severity):
            return FrameAnalysis(
                vehicle_count=9, density=60.0, occupancy=0.85, estimated_speed_kmh=4.0,
                congestion_level="jam",
                findings=[Finding("congestion", 0.8, severity, "standstill")],
            )

        pipeline.ingest(self.camera, analysis(0.7))
        pipeline.ingest(self.camera, analysis(0.9))

        self.assertEqual(RoadEvent.objects.count(), 1)
        self.assertAlmostEqual(RoadEvent.objects.first().severity, 0.9, places=3)

    def test_emergency_sightings_never_become_road_events(self):
        from apps.network.cv.detection import FrameAnalysis
        from apps.network.models import RoadEvent

        analysis = FrameAnalysis(
            vehicle_count=3, density=8.0, occupancy=0.2, estimated_speed_kmh=40.0,
            congestion_level="free",
            findings=[Finding("emergency_vehicle", 0.95, 0.0, "AMB-101 here")],
        )
        _, created = pipeline.ingest(self.camera, analysis)
        self.assertEqual(created, [])
        self.assertEqual(RoadEvent.objects.count(), 0)

    def test_sweep_survives_a_failing_camera(self):
        from unittest.mock import patch

        with patch.object(pipeline.backends, "detect", side_effect=RuntimeError("dead feed")):
            result = pipeline.sweep()
        self.assertEqual(result["cameras_failed"], 1)
        self.assertEqual(result["cameras_analysed"], 0)

    def test_still_tracks_forget_vehicles_that_leave(self):
        """Otherwise a departed vehicle stays 'parked' forever."""
        history = pipeline._update_still_tracks(
            self.camera.pk, [car(track=1, motion=0.0), car(track=2, motion=0.0)]
        )
        self.assertEqual(set(history), {1, 2})

        history = pipeline._update_still_tracks(self.camera.pk, [car(track=1, motion=0.0)])
        self.assertEqual(set(history), {1})
        self.assertEqual(history[1], 2)

    def test_a_moving_vehicle_resets_its_still_count(self):
        pipeline._update_still_tracks(self.camera.pk, [car(track=1, motion=0.0)])
        history = pipeline._update_still_tracks(self.camera.pk, [car(track=1, motion=0.5)])
        self.assertNotIn(1, history)
