"""Detection primitives and the frame analysis they compose into."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

from apps.core.enums import CongestionLevel, RoadEventType

#: COCO classes that count as road traffic for density estimation.
VEHICLE_CLASSES = frozenset({"car", "motorcycle", "bus", "truck", "bicycle", "train"})
#: Classes large enough that one blocking a lane matters on its own.
LARGE_VEHICLE_CLASSES = frozenset({"bus", "truck", "train"})
#: Emergency vehicles are buses/trucks in COCO terms; the class alone cannot
#: identify them, which is why `emergency.py` exists.
EMERGENCY_CANDIDATE_CLASSES = frozenset({"truck", "bus", "car"})

#: A camera sees roughly this much road. Used to convert a count into a density.
FIELD_OF_VIEW_M = 120.0
#: Greenshields jam density, vehicles per lane-kilometre.
JAM_DENSITY_PER_LANE_KM = 130.0


@dataclass
class Detection:
    """One detected object, in normalised frame coordinates."""

    label: str
    confidence: float
    #: (x1, y1, x2, y2), each in [0, 1].
    bbox: tuple[float, float, float, float]
    #: Set by the tracker across frames; None on a single-frame analysis.
    track_id: int | None = None
    #: Pixels/second of apparent motion, when a previous frame was available.
    motion: float | None = None

    @property
    def area(self) -> float:
        return max(0.0, self.bbox[2] - self.bbox[0]) * max(0.0, self.bbox[3] - self.bbox[1])

    @property
    def centre(self) -> tuple[float, float]:
        return ((self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2)

    @property
    def is_vehicle(self) -> bool:
        return self.label in VEHICLE_CLASSES

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "bbox": [round(v, 4) for v in self.bbox],
            "track_id": self.track_id,
            "motion": round(self.motion, 3) if self.motion is not None else None,
        }


@dataclass
class Finding:
    """Something the analysis concluded, with its evidence.

    ``evidence`` is the point: a control room asked to close a road on the word
    of a camera needs to see *why* the camera thinks so, and an operator who
    can read the reasoning can dismiss a false positive in seconds instead of
    dispatching a unit to check.
    """

    kind: str
    confidence: float
    severity: float
    summary: str
    evidence: list[str] = field(default_factory=list)
    #: Region of the frame this concerns, when it is localised.
    bbox: tuple[float, float, float, float] | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "confidence": round(self.confidence, 3),
            "severity": round(self.severity, 3),
            "summary": self.summary,
            "evidence": self.evidence,
            "bbox": [round(v, 4) for v in self.bbox] if self.bbox else None,
        }

    @property
    def is_actionable(self) -> bool:
        """Below this a finding is logged but does not create a road event.

        Chosen deliberately low-but-not-trivial: a false road closure reroutes
        ambulances away from a clear road, so the bar to *act* is higher than
        the bar to *notice*.
        """
        return self.confidence >= 0.55


@dataclass
class FrameAnalysis:
    """Everything one analysed frame says about a road."""

    vehicle_count: int
    density: float                  # vehicles per lane-km
    occupancy: float                # fraction of the carriageway covered
    estimated_speed_kmh: float
    congestion_level: str
    detections: list[Detection] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    emergency_vehicles: list[dict] = field(default_factory=list)
    backend: str = "simulated"
    #: Overall trust in this frame's analysis - low when few objects were seen
    #: or the detector was uncertain about the ones it did see.
    confidence: float = 0.5

    def findings_of(self, kind: str) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]

    @property
    def actionable_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.is_actionable]

    def as_dict(self) -> dict:
        return {
            "vehicle_count": self.vehicle_count,
            "density": round(self.density, 2),
            "occupancy": round(self.occupancy, 3),
            "estimated_speed_kmh": round(self.estimated_speed_kmh, 1),
            "congestion_level": self.congestion_level,
            "confidence": round(self.confidence, 3),
            "backend": self.backend,
            "emergency_vehicles": self.emergency_vehicles,
            "findings": [f.as_dict() for f in self.findings],
            "detection_count": len(self.detections),
        }


# ---------------------------------------------------------------------------
# Density and speed
# ---------------------------------------------------------------------------
def density_from_detections(vehicles: Iterable[Detection], lanes: int) -> float:
    """Vehicles per lane-kilometre from a single frame."""
    count = sum(1 for _ in vehicles)
    return count / max(0.1, lanes * FIELD_OF_VIEW_M / 1000.0)


def speed_from_density(density: float, design_kmh: float) -> float:
    """Greenshields: speed falls linearly as density approaches jam density.

    Crude compared with tracking vehicles across frames, but it needs only one
    frame, which is what makes it affordable to run across a whole camera
    estate on a schedule rather than continuously.
    """
    ratio = max(0.0, min(1.0, density / JAM_DENSITY_PER_LANE_KM))
    return max(3.0, design_kmh * (1.0 - ratio))


def occupancy_from_detections(vehicles: Iterable[Detection]) -> float:
    """Fraction of the frame covered by vehicles, capped at 1."""
    return min(1.0, sum(d.area for d in vehicles))


def congestion_from_speed(speed_kmh: float, design_kmh: float) -> str:
    return CongestionLevel.from_ratio(speed_kmh / design_kmh if design_kmh else 1.0)


def analysis_confidence(detections: list[Detection], vehicles: list[Detection]) -> float:
    """How much to trust this frame.

    Two things erode it: the detector being unsure about what it saw, and
    having seen almost nothing - an empty frame is equally consistent with a
    clear road and a camera pointing at a wall.
    """
    if not detections:
        return 0.35
    mean_confidence = sum(d.confidence for d in detections) / len(detections)
    # Confidence in the *aggregate* grows with the sample, saturating ~10.
    sample_factor = 1.0 - math.exp(-len(vehicles) / 4.0) if vehicles else 0.4
    return max(0.2, min(0.97, 0.45 * mean_confidence + 0.55 * (0.5 + 0.5 * sample_factor)))


#: Finding kind -> the road event it creates when actionable.
FINDING_TO_EVENT = {
    "accident": RoadEventType.ACCIDENT,
    "road_block": RoadEventType.BLOCKAGE,
    "illegal_parking": RoadEventType.ILLEGAL_PARKING,
    "congestion": RoadEventType.CONGESTION,
    "lane_obstruction": RoadEventType.LANE_OBSTRUCTION,
}
