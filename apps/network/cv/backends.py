"""Frame sources: real YOLOv8 inference, and a deterministic simulator.

Both return ``list[Detection]`` so nothing downstream knows which ran. The
simulator is not a stub - it produces plausible, internally consistent frames
driven by the segment's own live state and an Indian-metro demand curve, which
is what lets the whole six-detector pipeline be exercised and tested without a
GPU or a camera estate.
"""
from __future__ import annotations

import hashlib
import logging
import math

from django.conf import settings
from django.utils import timezone

from apps.network.cv.detection import (
    JAM_DENSITY_PER_LANE_KM,
    FIELD_OF_VIEW_M,
    Detection,
)

log = logging.getLogger("sevps.cv")

_yolo_model = None


def backend_name() -> str:
    """Which backend this process will actually use."""
    mode = settings.SEVPS["CV_MODE"].lower()
    if mode != "yolo":
        return "simulated"
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        return "simulated (yolo requested but ultralytics is not installed)"
    return f"yolo:{settings.SEVPS['CV_MODEL']}"


def is_yolo_active() -> bool:
    return backend_name().startswith("yolo")


# ---------------------------------------------------------------------------
# YOLOv8
# ---------------------------------------------------------------------------
def _load_yolo():
    global _yolo_model
    if _yolo_model is None:
        from ultralytics import YOLO

        _yolo_model = YOLO(settings.SEVPS["CV_MODEL"])
        log.info("loaded YOLO model %s", settings.SEVPS["CV_MODEL"])
    return _yolo_model


def detect_with_yolo(camera, frame=None) -> list[Detection]:
    """Run YOLOv8 on one frame, grabbing it from the stream if not supplied."""
    import cv2

    if frame is None:
        capture = cv2.VideoCapture(camera.stream_url)
        try:
            ok, frame = capture.read()
        finally:
            capture.release()
        if not ok:
            raise RuntimeError(f"could not read a frame from {camera.stream_url}")

    model = _load_yolo()
    results = model.predict(
        frame, conf=settings.SEVPS["CV_CONFIDENCE"], verbose=False
    )[0]
    height, width = frame.shape[:2]

    detections: list[Detection] = []
    for box in results.boxes:
        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
        detections.append(
            Detection(
                label=results.names[int(box.cls[0])],
                confidence=float(box.conf[0]),
                bbox=(x1 / width, y1 / height, x2 / width, y2 / height),
                # ultralytics assigns ids only in tracking mode; a single
                # predict() call has none, and claiming otherwise would make
                # the parking detector silently wrong.
                track_id=int(box.id[0]) if getattr(box, "id", None) is not None else None,
            )
        )
    return detections


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------
def _stable_unit(seed: str) -> float:
    """Deterministic value in [0, 1) from a seed string.

    Deterministic per (camera, minute) so repeated analysis inside the same
    minute is stable and tests are reproducible.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    return int.from_bytes(digest[:4], "big") / 0x100000000


def rush_hour_load(now=None) -> float:
    """Indian metro demand curve: morning and evening peaks, a lunch bump."""
    now = now or timezone.localtime()
    minutes = now.hour * 60 + now.minute
    peaks = ((9 * 60, 90, 1.0), (18 * 60 + 30, 110, 1.0), (13 * 60, 120, 0.35))
    load = 0.18
    for centre, width, weight in peaks:
        load += weight * math.exp(-(((minutes - centre) / width) ** 2))
    if now.weekday() >= 5:
        load *= 0.7
    return min(1.0, load)


def detect_simulated(camera, frame=None) -> list[Detection]:
    """Synthesise a frame consistent with the segment's live state.

    An existing measured speed dominates, so the simulation agrees with
    whatever the rest of the platform already believes about this road; only
    when there is no reading does the demand curve drive it.
    """
    now = timezone.localtime()
    segment = camera.segment
    lanes = segment.lanes if segment else 2
    design = segment.design_speed_kmh if segment else 40.0

    jitter = _stable_unit(f"{camera.pk}:{now:%Y%m%d%H%M}")
    load = max(0.02, min(0.99, rush_hour_load(now) + (jitter - 0.5) * 0.24))

    if segment and segment.current_speed_kmh:
        occupancy = max(0.02, min(0.95, 1.0 - segment.current_speed_kmh / design))
    else:
        occupancy = load * 0.9

    density = occupancy * JAM_DENSITY_PER_LANE_KM
    vehicle_count = max(0, int(density * lanes * (FIELD_OF_VIEW_M / 1000.0)))

    detections: list[Detection] = []
    for index in range(vehicle_count):
        spot = _stable_unit(f"{camera.pk}:{now:%Y%m%d%H%M}:{index}")
        label = "car" if spot > 0.22 else ("motorcycle" if spot > 0.1 else "truck")
        # Lay vehicles out in lanes so bounding boxes are plausible rather
        # than random - the detectors reason about geometry.
        lane = index % max(1, lanes)
        row = index // max(1, lanes)
        width, height = 0.11, 0.075
        x1 = min(0.88, 0.04 + lane * (0.92 / max(1, lanes)))
        y1 = min(0.9, 0.05 + (row * 0.1) % 0.85)
        detections.append(
            Detection(
                label=label,
                confidence=0.62 + 0.3 * spot,
                bbox=(x1, y1, x1 + width, y1 + height),
                track_id=index,
                # Congested traffic barely moves; free-flowing traffic does.
                motion=max(0.0, (1.0 - occupancy) * 0.35 * spot),
            )
        )

    # A jammed road with almost nothing moving occasionally has people out of
    # their cars - the accident signature the detector is meant to find.
    if occupancy > 0.6 and jitter > 0.93:
        detections.append(
            Detection(label="person", confidence=0.71, bbox=(0.44, 0.5, 0.49, 0.66), motion=0.0)
        )

    return detections


def detect(camera, frame=None) -> tuple[list[Detection], str]:
    """Run whichever backend is available, falling back rather than failing.

    A camera that cannot be decoded must not take the sweep down with it - the
    other cameras still have useful things to say.
    """
    if is_yolo_active():
        try:
            return detect_with_yolo(camera, frame), "yolo"
        except Exception:
            log.warning("YOLO analysis failed for %s; simulating", camera, exc_info=True)
    return detect_simulated(camera, frame), "simulated"
