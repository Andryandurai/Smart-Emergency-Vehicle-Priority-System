"""Computer-vision traffic analysis (feature 4.5).

Existing municipal camera feeds are analysed to derive vehicle density,
congestion, blockages, illegal parking and accidents.  Two backends:

``yolo``      real YOLOv8 inference via ultralytics + OpenCV.
``simulated`` deterministic synthetic detections driven by the segment's own
              live state and time of day - lets the whole platform be
              demonstrated and tested without GPUs or live camera access.

Both return the same :class:`FrameAnalysis`, so nothing downstream knows or
cares which one ran.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from apps.core.enums import CongestionLevel, EventSource, RoadEventType

log = logging.getLogger("sevps.vision")

#: COCO classes that count as road traffic for density estimation.
VEHICLE_CLASSES = {"car", "motorcycle", "bus", "truck", "bicycle", "train"}
_yolo_model = None


@dataclass
class Detection:
    label: str
    confidence: float
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2 normalised


@dataclass
class FrameAnalysis:
    """What one analysed frame tells us about a road."""

    vehicle_count: int
    density: float                 # vehicles per lane-km
    occupancy: float               # fraction of the road area covered
    estimated_speed_kmh: float
    congestion_level: str
    detections: list[Detection] = field(default_factory=list)
    incidents: list[dict] = field(default_factory=list)
    backend: str = "simulated"

    def as_dict(self) -> dict:
        return {
            "vehicle_count": self.vehicle_count,
            "density": round(self.density, 2),
            "occupancy": round(self.occupancy, 3),
            "estimated_speed_kmh": round(self.estimated_speed_kmh, 1),
            "congestion_level": self.congestion_level,
            "incidents": self.incidents,
            "backend": self.backend,
        }


def cv_backend() -> str:
    """Which CV backend is actually usable in this process."""
    mode = settings.SEVPS["CV_MODE"].lower()
    if mode != "yolo":
        return "simulated"
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        return "simulated (yolo requested but ultralytics not installed)"
    return f"yolo:{settings.SEVPS['CV_MODEL']}"


# ---------------------------------------------------------------------------
# YOLOv8 backend
# ---------------------------------------------------------------------------
def _load_yolo():
    global _yolo_model
    if _yolo_model is None:
        from ultralytics import YOLO

        _yolo_model = YOLO(settings.SEVPS["CV_MODEL"])
        log.info("Loaded YOLO model %s", settings.SEVPS["CV_MODEL"])
    return _yolo_model


def _analyse_with_yolo(camera, frame=None) -> FrameAnalysis:
    import cv2

    if frame is None:
        capture = cv2.VideoCapture(camera.stream_url)
        try:
            ok, frame = capture.read()
        finally:
            capture.release()
        if not ok:
            raise RuntimeError(f"could not read frame from {camera.stream_url}")

    model = _load_yolo()
    conf = settings.SEVPS["CV_CONFIDENCE"]
    results = model.predict(frame, conf=conf, verbose=False)[0]
    height, width = frame.shape[:2]

    detections: list[Detection] = []
    for box in results.boxes:
        label = results.names[int(box.cls[0])]
        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
        detections.append(
            Detection(
                label=label,
                confidence=float(box.conf[0]),
                bbox=(x1 / width, y1 / height, x2 / width, y2 / height),
            )
        )

    vehicles = [d for d in detections if d.label in VEHICLE_CLASSES]
    occupancy = min(
        1.0,
        sum((d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1]) for d in vehicles),
    )
    return _build_analysis(camera, vehicles, occupancy, detections, backend="yolo")


# ---------------------------------------------------------------------------
# Simulated backend
# ---------------------------------------------------------------------------
def _stable_jitter(seed: str, spread: float) -> float:
    """Deterministic pseudo-random value in [-spread, +spread].

    Deterministic per (camera, minute) so repeated analysis inside the same
    minute is stable and tests are reproducible.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # [0, 1]
    return (unit * 2 - 1) * spread


def _rush_hour_factor(now) -> float:
    """Indian metro demand curve: morning and evening peaks."""
    minutes = now.hour * 60 + now.minute
    peaks = ((9 * 60, 90, 1.0), (18 * 60 + 30, 110, 1.0), (13 * 60, 120, 0.35))
    load = 0.18
    for centre, width, weight in peaks:
        load += weight * math.exp(-(((minutes - centre) / width) ** 2))
    if now.weekday() >= 5:
        load *= 0.7
    return min(1.0, load)


def _analyse_simulated(camera, frame=None) -> FrameAnalysis:
    now = timezone.localtime()
    segment = camera.segment
    lanes = segment.lanes if segment else 2
    load = _rush_hour_factor(now)
    load = max(0.02, min(0.99, load + _stable_jitter(f"{camera.pk}:{now:%Y%m%d%H%M}", 0.12)))

    # An existing measured speed dominates; otherwise demand drives occupancy.
    if segment and segment.current_speed_kmh:
        ratio = segment.current_speed_kmh / segment.design_speed_kmh
        occupancy = max(0.02, min(0.95, 1.0 - ratio))
    else:
        occupancy = load * 0.9

    # Greenshields: speed falls linearly as density approaches jam density.
    design = segment.design_speed_kmh if segment else 40.0
    speed = max(3.0, design * (1.0 - occupancy) ** 0.85)

    # Vehicles visible in one camera field of view (~120 m of road).
    view_len_m = 120.0
    jam_density_per_lane_km = 130.0
    density = occupancy * jam_density_per_lane_km
    vehicle_count = int(density * lanes * (view_len_m / 1000.0))

    detections = [
        Detection("car", 0.9, (0.0, 0.0, 0.0, 0.0)) for _ in range(max(0, vehicle_count))
    ]
    return _build_analysis(camera, detections, occupancy, detections, backend="simulated", speed=speed)


# ---------------------------------------------------------------------------
# Shared post-processing
# ---------------------------------------------------------------------------
def _build_analysis(
    camera,
    vehicles: list[Detection],
    occupancy: float,
    all_detections: list[Detection],
    *,
    backend: str,
    speed: float | None = None,
) -> FrameAnalysis:
    segment = camera.segment
    lanes = segment.lanes if segment else 2
    design = segment.design_speed_kmh if segment else 40.0
    view_len_m = 120.0

    density = len(vehicles) / max(0.1, lanes * view_len_m / 1000.0)
    if speed is None:
        # Greenshields fundamental diagram, clamped to sane bounds.
        jam_density = 130.0
        speed = max(3.0, design * max(0.05, 1.0 - density / jam_density))

    incidents = _infer_incidents(camera, all_detections, occupancy, speed, design)
    return FrameAnalysis(
        vehicle_count=len(vehicles),
        density=density,
        occupancy=occupancy,
        estimated_speed_kmh=speed,
        congestion_level=CongestionLevel.from_ratio(speed / design if design else 1.0),
        detections=all_detections[:200],
        incidents=incidents,
        backend=backend,
    )


def _infer_incidents(camera, detections, occupancy, speed, design) -> list[dict]:
    """Translate raw detections into the road events the router understands."""
    incidents: list[dict] = []
    ratio = speed / design if design else 1.0

    if ratio < 0.15 and occupancy > 0.55:
        incidents.append(
            {
                "event_type": RoadEventType.CONGESTION,
                "severity": round(min(0.9, 1.0 - ratio), 2),
                "confidence": 0.7,
                "description": "Standstill traffic detected on camera",
            }
        )

    # A person or a stationary heavy vehicle in the carriageway with the road
    # otherwise empty is the classic signature of a crash or a breakdown.
    labels = {d.label for d in detections}
    if "person" in labels and occupancy < 0.4 and ratio < 0.4:
        incidents.append(
            {
                "event_type": RoadEventType.ACCIDENT,
                "severity": 0.75,
                "confidence": 0.45,
                "description": "Pedestrians in carriageway with stopped traffic - possible accident",
            }
        )

    if occupancy > 0.85:
        incidents.append(
            {
                "event_type": RoadEventType.BLOCKAGE,
                "severity": 0.85,
                "confidence": 0.5,
                "description": "Carriageway appears blocked",
            }
        )

    return incidents


def analyse_camera(camera, frame=None) -> FrameAnalysis:
    """Analyse one camera, falling back to simulation if YOLO is unavailable."""
    if cv_backend().startswith("yolo"):
        try:
            return _analyse_with_yolo(camera, frame)
        except Exception:
            log.warning("YOLO analysis failed for camera %s; simulating", camera, exc_info=True)
    return _analyse_simulated(camera, frame)


def ingest_camera_analysis(camera, analysis: FrameAnalysis):
    """Persist an analysis: observation row, segment speed, and any incidents."""
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

    created_events = []
    for incident in analysis.incidents:
        # Don't spam duplicates - one open event per (camera, type).
        existing = RoadEvent.objects.active().filter(
            event_type=incident["event_type"],
            segment_id=camera.segment_id,
            source=EventSource.COMPUTER_VISION,
        ).first()
        if existing:
            existing.severity = incident["severity"]
            existing.confidence = incident["confidence"]
            existing.save(update_fields=["severity", "confidence", "updated_at"])
            continue
        created_events.append(
            RoadEvent.objects.create(
                event_type=incident["event_type"],
                segment=camera.segment,
                latitude=camera.latitude,
                longitude=camera.longitude,
                severity=incident["severity"],
                confidence=incident["confidence"],
                description=incident["description"],
                source=EventSource.COMPUTER_VISION,
            )
        )

    camera.last_analysed_at = timezone.now()
    camera.save(update_fields=["last_analysed_at", "updated_at"])
    return observation, created_events
