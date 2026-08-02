"""Compose detection, the six analysers and persistence into one sweep.

One frame in, one :class:`FrameAnalysis` out, then the side effects: an
observation row, the segment's live speed, and a road event for any finding
confident enough to act on.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.core.enums import EventSource
from apps.network.cv import analysers, backends
from apps.network.cv.detection import (
    FINDING_TO_EVENT,
    Detection,
    FrameAnalysis,
    analysis_confidence,
    congestion_from_speed,
    density_from_detections,
    occupancy_from_detections,
    speed_from_density,
)

log = logging.getLogger("sevps.cv.pipeline")

#: camera id -> {track_id: consecutive analyses seen stationary}
#: In-process only. Losing it on restart costs a few minutes of parking
#: evidence, which is a fair trade for not adding a table for it.
_still_tracks: dict[int, dict[int, int]] = {}
#: How near a fleet vehicle must be for this camera to plausibly see it.
FLEET_SEARCH_RADIUS_M = 250.0


def reset_tracking() -> None:
    """Forget stationary-vehicle history. Used by tests."""
    _still_tracks.clear()


def _update_still_tracks(camera_id: int, detections: list[Detection]) -> dict[int, int]:
    """Count how many consecutive analyses each tracked vehicle sat still.

    Persistence is what separates a parked vehicle from one waiting at a red
    light, so it has to be counted across sweeps rather than within a frame.
    """
    history = _still_tracks.setdefault(camera_id, {})
    seen: set[int] = set()

    for detection in detections:
        if detection.track_id is None or not detection.is_vehicle:
            continue
        seen.add(detection.track_id)
        if detection.motion is not None and detection.motion < 0.02:
            history[detection.track_id] = history.get(detection.track_id, 0) + 1
        else:
            history.pop(detection.track_id, None)

    # A track that left the frame is no longer evidence of anything.
    for track_id in set(history) - seen:
        history.pop(track_id, None)
    return history


def _nearby_fleet(camera) -> list[dict]:
    from apps.fleet.models import EmergencyVehicle

    found = EmergencyVehicle.objects.on_mission().near(
        camera.latitude, camera.longitude, FLEET_SEARCH_RADIUS_M
    )
    return [
        {
            "callsign": vehicle.callsign,
            "priority_level": vehicle.priority_level,
            "distance_m": vehicle.distance_m,
        }
        for vehicle in found
    ]


def analyse_camera(camera, frame=None) -> FrameAnalysis:
    """Run every detector over one frame."""
    detections, backend = backends.detect(camera, frame)

    segment = camera.segment
    lanes = segment.lanes if segment else 2
    design = segment.design_speed_kmh if segment else 40.0

    vehicles = [d for d in detections if d.is_vehicle]
    density = density_from_detections(vehicles, lanes)
    occupancy = occupancy_from_detections(vehicles)
    speed = speed_from_density(density, design)
    speed_ratio = speed / design if design else 1.0

    still_tracks = _update_still_tracks(camera.pk, detections)

    findings = []
    for finding in (
        analysers.detect_accident(detections, occupancy=occupancy, speed_ratio=speed_ratio),
        analysers.detect_road_block(
            detections, occupancy=occupancy, speed_ratio=speed_ratio, lanes=lanes
        ),
        analysers.detect_illegal_parking(
            detections, still_tracks=still_tracks, speed_ratio=speed_ratio
        ),
        analysers.detect_congestion(occupancy=occupancy, speed_ratio=speed_ratio),
    ):
        if finding is not None:
            findings.append(finding)

    sightings, emergency_findings = analysers.detect_emergency_vehicles(
        detections, nearby_fleet=_nearby_fleet(camera)
    )
    findings.extend(emergency_findings)

    return FrameAnalysis(
        vehicle_count=len(vehicles),
        density=density,
        occupancy=occupancy,
        estimated_speed_kmh=speed,
        congestion_level=congestion_from_speed(speed, design),
        detections=detections[:200],
        findings=findings,
        emergency_vehicles=sightings,
        backend=backend,
        confidence=analysis_confidence(detections, vehicles),
    )


def ingest(camera, analysis: FrameAnalysis):
    """Persist an analysis: observation, segment speed, and any road events.

    Only *actionable* findings become road events. A low-confidence accident
    guess is recorded on the analysis for a human to look at; it does not
    reroute ambulances off a road that may be perfectly clear.
    """
    from apps.network.models import RoadEvent, TrafficObservation

    observation = None
    if camera.segment_id:
        observation = TrafficObservation.objects.create(
            segment=camera.segment,
            speed_kmh=analysis.estimated_speed_kmh,
            vehicle_count=analysis.vehicle_count,
            density=analysis.density,
            occupancy=analysis.occupancy,
            congestion_level=analysis.congestion_level,
            source=EventSource.COMPUTER_VISION,
            camera=camera,
        )
        camera.segment.apply_speed(analysis.estimated_speed_kmh)

    created: list[RoadEvent] = []
    for finding in analysis.actionable_findings:
        event_type = FINDING_TO_EVENT.get(finding.kind)
        if event_type is None:          # e.g. emergency_vehicle: informational
            continue

        # One open event per (segment, type, source) - a camera reporting the
        # same jam every 30 seconds must not create a hundred road events.
        existing = (
            RoadEvent.objects.active()
            .filter(
                event_type=event_type,
                segment_id=camera.segment_id,
                source=EventSource.COMPUTER_VISION,
            )
            .first()
        )
        if existing:
            existing.severity = finding.severity
            existing.confidence = finding.confidence
            existing.description = finding.summary
            existing.save(
                update_fields=["severity", "confidence", "description", "updated_at"]
            )
            continue

        created.append(
            RoadEvent.objects.create(
                event_type=event_type,
                segment=camera.segment,
                latitude=camera.latitude,
                longitude=camera.longitude,
                severity=finding.severity,
                confidence=finding.confidence,
                description=f"{finding.summary} ({'; '.join(finding.evidence[:2])})",
                source=EventSource.COMPUTER_VISION,
            )
        )

    camera.last_analysed_at = timezone.now()
    camera.save(update_fields=["last_analysed_at", "updated_at"])
    return observation, created


def sweep(cameras=None) -> dict:
    """Analyse every active camera. Never lets one bad feed stop the rest."""
    from apps.network.models import CameraFeed

    if cameras is None:
        cameras = CameraFeed.objects.filter(is_active=True).select_related("segment")

    analysed = events = failed = 0
    emergency_sightings: list[dict] = []
    findings_by_kind: dict[str, int] = {}

    for camera in cameras:
        try:
            analysis = analyse_camera(camera)
        except Exception:
            log.warning("camera %s failed", camera, exc_info=True)
            failed += 1
            continue

        _, created = ingest(camera, analysis)
        analysed += 1
        events += len(created)
        emergency_sightings.extend(analysis.emergency_vehicles)
        for finding in analysis.findings:
            findings_by_kind[finding.kind] = findings_by_kind.get(finding.kind, 0) + 1

    return {
        "cameras_analysed": analysed,
        "cameras_failed": failed,
        "events_created": events,
        "findings": findings_by_kind,
        "emergency_sightings": emergency_sightings,
        "backend": backends.backend_name(),
    }
