"""The six detectors, each turning detections into a Finding with evidence.

Each is a pure function of the detections plus road context, so they can be
tested against hand-built frames without a model, a camera or a database.
"""
from __future__ import annotations

from apps.network.cv.detection import (
    EMERGENCY_CANDIDATE_CLASSES,
    LARGE_VEHICLE_CLASSES,
    Detection,
    Finding,
)

# ---------------------------------------------------------------------------
# Emergency vehicle detection
# ---------------------------------------------------------------------------
#: A vehicle this close to a camera's known position is plausibly the one the
#: camera can see. Loose, because GPS in an urban canyon is not tight.
FLEET_MATCH_RADIUS_M = 250.0


def detect_emergency_vehicles(
    detections: list[Detection],
    *,
    nearby_fleet: list[dict],
) -> tuple[list[dict], list[Finding]]:
    """Identify emergency vehicles in frame.

    Deliberately **not** a visual classifier. COCO has no ambulance class, and
    a bespoke one trained on a few hundred crops would be unreliable in exactly
    the conditions that matter - rain, night, partial occlusion.

    SEVPS already knows where every emergency vehicle is to within a few metres
    and updates that several times a minute. So the strong signal is the fleet
    position, and vision is used to *corroborate* it: a large vehicle in frame
    at the moment telemetry says one of ours is here is a confirmed sighting.

    That inverts the usual arrangement, and it is the right way round here -
    a GPS fix is far better evidence than a bounding box, and the box adds
    what GPS cannot: confirmation the vehicle is actually on this carriageway
    rather than on a parallel road ten metres away.
    """
    if not nearby_fleet:
        return [], []

    candidates = [
        d for d in detections
        if d.label in EMERGENCY_CANDIDATE_CLASSES and d.confidence >= 0.4
    ]
    large = [d for d in candidates if d.label in LARGE_VEHICLE_CLASSES]

    sightings: list[dict] = []
    findings: list[Finding] = []

    for vehicle in nearby_fleet:
        distance = vehicle.get("distance_m", FLEET_MATCH_RADIUS_M)
        # Telemetry proximity is the base evidence; a matching large vehicle in
        # frame raises it, a completely empty frame lowers it.
        confidence = max(0.3, 1.0 - distance / FLEET_MATCH_RADIUS_M)
        evidence = [
            f"{vehicle['callsign']} telemetry places it {distance:.0f} m from this camera",
        ]
        if large:
            confidence = min(0.97, confidence + 0.2)
            evidence.append(f"{len(large)} large vehicle(s) visible in frame")
        elif not candidates:
            confidence *= 0.5
            evidence.append("no matching vehicle visible - possible occlusion or wrong approach")

        sightings.append(
            {
                "callsign": vehicle["callsign"],
                "priority_level": vehicle.get("priority_level"),
                "distance_m": round(distance, 1),
                "confidence": round(confidence, 3),
                "visually_corroborated": bool(large),
            }
        )
        findings.append(
            Finding(
                kind="emergency_vehicle",
                confidence=confidence,
                severity=0.0,          # informational, not a road problem
                summary=(
                    f"{vehicle['callsign']} (level {vehicle.get('priority_level')}) "
                    f"at this junction"
                ),
                evidence=evidence,
                bbox=large[0].bbox if large else None,
            )
        )
    return sightings, findings


# ---------------------------------------------------------------------------
# Road block detection
# ---------------------------------------------------------------------------
def detect_road_block(
    detections: list[Detection], *, occupancy: float, speed_ratio: float, lanes: int
) -> Finding | None:
    """The carriageway is impassable.

    Distinguished from heavy congestion by *stillness*: a jam is full of slowly
    moving vehicles, a blockage is full of stationary ones, often with people
    out of their cars. Getting this wrong is expensive - a false blockage
    reroutes ambulances off a usable road.
    """
    if occupancy < 0.7 or speed_ratio > 0.2:
        return None

    stationary = [d for d in detections if d.motion is not None and d.motion < 0.02]
    people = [d for d in detections if d.label == "person"]
    vehicles = [d for d in detections if d.is_vehicle]

    evidence = [
        f"carriageway {occupancy:.0%} occupied",
        f"traffic at {speed_ratio:.0%} of free-flow speed",
    ]
    confidence = 0.5 + 0.25 * min(1.0, occupancy)

    if stationary and vehicles:
        share = len(stationary) / len(vehicles)
        if share > 0.8:
            confidence = min(0.95, confidence + 0.2)
            evidence.append(f"{share:.0%} of vehicles completely stationary")
    if people:
        confidence = min(0.96, confidence + 0.1)
        evidence.append(f"{len(people)} person(s) in the carriageway")

    return Finding(
        kind="road_block",
        confidence=confidence,
        severity=min(0.95, 0.6 + 0.35 * occupancy),
        summary=f"Carriageway appears blocked across {lanes} lane(s)",
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Illegal parking
# ---------------------------------------------------------------------------
#: A vehicle must be still for this many consecutive analyses to count as
#: parked rather than merely queuing.
PARKED_OBSERVATIONS = 3


def detect_illegal_parking(
    detections: list[Detection], *, still_tracks: dict[int, int], speed_ratio: float
) -> Finding | None:
    """A stationary vehicle in a live lane.

    The hard part is not spotting a still vehicle - it is telling one apart
    from a vehicle waiting at a red light. Two things separate them: a parked
    vehicle stays still while *traffic around it moves*, and it stays still
    across several analyses rather than one.
    """
    if speed_ratio < 0.4:
        # Traffic is slow anyway; everything looks parked. Not a useful call.
        return None

    persistent = [
        track_id for track_id, count in still_tracks.items() if count >= PARKED_OBSERVATIONS
    ]
    if not persistent:
        return None

    tracked = {d.track_id: d for d in detections if d.track_id is not None}
    offenders = [tracked[t] for t in persistent if t in tracked]
    if not offenders:
        return None

    return Finding(
        kind="illegal_parking",
        confidence=min(0.9, 0.5 + 0.12 * len(persistent)),
        # Real but minor: it narrows the road rather than closing it.
        severity=min(0.4, 0.15 + 0.08 * len(persistent)),
        summary=f"{len(offenders)} vehicle(s) stationary while traffic flows around them",
        evidence=[
            f"{len(persistent)} vehicle(s) still for {PARKED_OBSERVATIONS}+ consecutive analyses",
            f"surrounding traffic at {speed_ratio:.0%} of free-flow speed",
        ],
        bbox=offenders[0].bbox,
    )


# ---------------------------------------------------------------------------
# Accident detection
# ---------------------------------------------------------------------------
def detect_accident(
    detections: list[Detection], *, occupancy: float, speed_ratio: float
) -> Finding | None:
    """The signature of a collision.

    No single cue is reliable, so this looks for a *combination*: people out of
    their vehicles, on a road that has stopped, that is not simply full of
    traffic. Each cue alone is common and innocent - pedestrians near a
    crossing, a red light, a busy junction - and the conjunction is not.

    Confidence stays deliberately moderate even when all cues fire. This
    dispatches units; it should prompt a human to look at the camera, not
    close a road on its own.
    """
    people = [d for d in detections if d.label == "person" and d.confidence >= 0.45]
    vehicles = [d for d in detections if d.is_vehicle]

    if not people or speed_ratio > 0.4:
        return None

    cues: list[str] = [
        f"{len(people)} person(s) detected in the carriageway",
        f"traffic at {speed_ratio:.0%} of free-flow speed",
    ]
    confidence = 0.35

    # People on a *clear* road is the strong signal. People on a jammed road is
    # a pavement, a market, or a crossing.
    if occupancy < 0.45:
        confidence += 0.2
        cues.append("road is not congested, so stopped traffic is unexplained")
    else:
        cues.append("road is congested - pedestrians may be unrelated")
        confidence -= 0.1

    stationary = [d for d in vehicles if d.motion is not None and d.motion < 0.02]
    if stationary and len(stationary) >= 2:
        confidence += 0.15
        cues.append(f"{len(stationary)} stationary vehicle(s) clustered")

    # Vehicles at odd angles cannot be read from an axis-aligned box, but an
    # unusually wide box for a car is a weak proxy for one turned across a lane.
    sideways = [
        d for d in vehicles
        if d.label == "car" and (d.bbox[2] - d.bbox[0]) > 2.2 * (d.bbox[3] - d.bbox[1])
    ]
    if sideways:
        confidence += 0.1
        cues.append(f"{len(sideways)} vehicle(s) at an unusual orientation")

    confidence = max(0.0, min(0.85, confidence))
    if confidence < 0.3:
        return None

    return Finding(
        kind="accident",
        confidence=confidence,
        severity=0.75,
        summary="Possible collision - pedestrians in carriageway with stopped traffic",
        evidence=cues,
        bbox=people[0].bbox,
    )


# ---------------------------------------------------------------------------
# Congestion (as a finding, distinct from the measured level)
# ---------------------------------------------------------------------------
def detect_congestion(*, occupancy: float, speed_ratio: float) -> Finding | None:
    """Standstill worth publishing as a road event.

    The measured congestion level is written on every observation regardless;
    this is only for the case severe enough that the router should avoid the
    road rather than merely cost it higher.
    """
    if speed_ratio > 0.15 or occupancy < 0.55:
        return None
    return Finding(
        kind="congestion",
        confidence=min(0.9, 0.55 + occupancy * 0.4),
        severity=min(0.9, 1.0 - speed_ratio),
        summary="Standstill traffic",
        evidence=[
            f"traffic at {speed_ratio:.0%} of free-flow speed",
            f"carriageway {occupancy:.0%} occupied",
        ],
    )
