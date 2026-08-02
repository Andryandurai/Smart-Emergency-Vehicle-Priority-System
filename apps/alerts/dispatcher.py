"""Alert targeting and fan-out (Layer 4).

Targeting rule: warn the road *ahead*, not the road around.  Alerts are
generated at sample points along the vehicle's planned route between now and
``DRIVER_ALERT_MAX_ETA_S`` seconds from now, each carrying the number of
seconds until the vehicle actually reaches that point.  A driver 15 seconds
ahead gets "15 seconds"; a driver the ambulance has already passed gets
nothing at all.

De-duplication matters as much as targeting: at a 2 s telemetry rate a naive
implementation would re-issue the same alert 30 times per junction.  Alerts
are therefore keyed by (trip, geohash cell) and refreshed rather than
duplicated.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

from apps.core.enums import AlertChannel, PriorityLevel
from apps.core.geo import Point, bearing_deg, geohash, haversine_m
from apps.core.realtime import broadcast, broadcast_ops, driver_group
from apps.alerts.models import DisplayBoard, DriverAlert

log = logging.getLogger("sevps.alerts")

#: Where along the look-ahead window to place alerts, in seconds.
SAMPLE_OFFSETS_S = (15, 30, 45, 60, 90)
#: An alert for a given cell is refreshed, not re-created, within this window.
DEDUPE_WINDOW_S = 20.0
#: How far from the route a display board may be and still be relevant.
BOARD_MATCH_RADIUS_M = 400.0


def build_message(seconds_away: float, priority_level: int, vehicle_type: str) -> tuple[str, str]:
    """Compose the on-screen text.  Returns ``(message, instruction)``."""
    label = {
        "ambulance": "Ambulance",
        "fire_engine": "Fire Engine",
        "police": "Police Vehicle",
        "disaster": "Emergency Unit",
    }.get(vehicle_type, "Emergency Vehicle")

    instruction = (
        "Please move to the left lane"
        if priority_level <= PriorityLevel.HIGH
        else "Please keep the left lane clear"
    )
    message = (
        f"{label} Approaching - {instruction}. "
        f"Estimated arrival: {int(round(seconds_away))} seconds."
    )
    return message, instruction


def broadcast_driver_alerts(trip, progress=None) -> dict:
    """Issue advance warnings ahead of a trip's vehicle.

    Returns a summary of what was issued; safe to call on every GPS tick.
    """
    from apps.brain.eta import compute_progress
    from apps.core.enums import TripStage

    cfg = settings.SEVPS
    plan = trip.active_route
    if plan is None or trip.stage in {TripStage.ARRIVED, TripStage.HANDOVER, TripStage.CANCELLED}:
        return {"issued": 0, "reason": "no active route"}

    # Level 4 transports run as ordinary traffic - warning drivers about them
    # would be noise, and noise is what makes real warnings ignorable.
    if trip.priority_level >= PriorityLevel.NON_CRITICAL:
        return {"issued": 0, "reason": "Level 4 transport - no driver alerts"}

    progress = progress or compute_progress(plan, trip.vehicle.point)
    if not progress.is_on_route:
        return {"issued": 0, "reason": "vehicle off route"}

    horizon = cfg["DRIVER_ALERT_MAX_ETA_S"]
    radius = cfg["DRIVER_ALERT_RADIUS_M"]
    now = timezone.now()

    issued, refreshed = [], 0
    seen_cells: set[str] = set()

    for offset in SAMPLE_OFFSETS_S:
        if offset > horizon:
            continue
        point = _point_ahead(plan, progress, offset)
        if point is None:
            continue

        cell = geohash(point.lat, point.lon, 6)
        if cell in seen_cells:
            continue
        seen_cells.add(cell)

        approach = bearing_deg(
            trip.vehicle.latitude, trip.vehicle.longitude, point.lat, point.lon
        )
        message, instruction = build_message(offset, trip.priority_level, trip.vehicle.vehicle_type)

        recent = DriverAlert.objects.filter(
            trip=trip,
            geohash=cell,
            created_at__gte=now - timezone.timedelta(seconds=DEDUPE_WINDOW_S),
        ).first()

        if recent is not None:
            recent.eta_seconds = offset
            recent.message = message
            recent.expires_at = now + timezone.timedelta(seconds=max(offset, 20))
            recent.approach_bearing_deg = approach
            recent.save(
                update_fields=[
                    "eta_seconds", "message", "expires_at",
                    "approach_bearing_deg", "updated_at",
                ]
            )
            alert = recent
            refreshed += 1
        else:
            alert = DriverAlert.objects.create(
                trip=trip,
                channel=AlertChannel.MOBILE_APP,
                message=message,
                instruction=instruction,
                eta_seconds=offset,
                radius_m=radius,
                approach_bearing_deg=approach,
                priority_level=trip.priority_level,
                latitude=point.lat,
                longitude=point.lon,
                geohash=cell,
                expires_at=now + timezone.timedelta(seconds=max(offset, 20)),
            )
            issued.append(alert)

        # Fan out to every road user in that cell.
        broadcast(driver_group(point.lat, point.lon), "driver_alert", alert.as_payload())

    boards = _update_display_boards(trip, plan, progress, horizon)

    if issued:
        broadcast_ops(
            "driver_alerts",
            {
                "trip_id": trip.id,
                "vehicle": trip.vehicle.callsign,
                "alerts": [a.as_payload() for a in issued],
            },
        )

    return {
        "issued": len(issued),
        "refreshed": refreshed,
        "cells": sorted(seen_cells),
        "boards_updated": boards,
    }


def _point_ahead(plan, progress, seconds: float) -> Point | None:
    """Position on the planned route ``seconds`` from now."""
    from apps.brain.eta import _distance_for_time  # internal by design

    target_offset = progress.elapsed_offset_s + seconds
    if target_offset >= plan.total_duration_s:
        return None
    distance = _distance_for_time(plan, target_offset)
    if distance >= plan.total_distance_m:
        return None

    from apps.core.geo import point_along_polyline

    return point_along_polyline([Point(lat, lon) for lat, lon in plan.geometry], distance)


def _update_display_boards(trip, plan, progress, horizon_s: float) -> int:
    """Push the warning onto roadside VMS boards the vehicle is approaching."""
    upcoming_points = []
    for offset in SAMPLE_OFFSETS_S:
        if offset > horizon_s:
            continue
        point = _point_ahead(plan, progress, offset)
        if point is not None:
            upcoming_points.append((offset, point))
    if not upcoming_points:
        return 0

    now = timezone.now()
    updated = 0
    for board in DisplayBoard.objects.filter(is_active=True):
        best = min(
            upcoming_points,
            key=lambda item: haversine_m(
                board.latitude, board.longitude, item[1].lat, item[1].lon
            ),
        )
        offset, point = best
        distance = haversine_m(board.latitude, board.longitude, point.lat, point.lon)
        if distance > BOARD_MATCH_RADIUS_M:
            continue

        message, instruction = build_message(offset, trip.priority_level, trip.vehicle.vehicle_type)
        board.current_message = message
        board.message_expires_at = now + timezone.timedelta(seconds=max(offset, 20))
        board.save(update_fields=["current_message", "message_expires_at", "updated_at"])

        DriverAlert.objects.create(
            trip=trip,
            channel=board.channel,
            board=board,
            message=message,
            instruction=instruction,
            eta_seconds=offset,
            radius_m=BOARD_MATCH_RADIUS_M,
            priority_level=trip.priority_level,
            latitude=board.latitude,
            longitude=board.longitude,
            geohash=geohash(board.latitude, board.longitude, 6),
            expires_at=board.message_expires_at,
        )
        broadcast_ops(
            "display_board",
            {
                "board": board.code,
                "message": message,
                "expires_at": board.message_expires_at,
                "latitude": board.latitude,
                "longitude": board.longitude,
            },
        )
        updated += 1
    return updated


def clear_expired_boards() -> int:
    """Blank display boards whose message has expired."""
    now = timezone.now()
    stale = DisplayBoard.objects.filter(
        message_expires_at__lt=now
    ).exclude(current_message="")
    count = stale.count()
    stale.update(current_message="", message_expires_at=None)
    if count:
        broadcast_ops("display_boards_cleared", {"count": count})
    return count


def active_alerts_near(lat: float, lon: float, radius_m: float = 1500) -> list[dict]:
    """Live alerts relevant to a road user at a position (mobile app poll path)."""
    now = timezone.now()
    alerts = DriverAlert.objects.filter(expires_at__gt=now).near(lat, lon, radius_m)
    return [a.as_payload() for a in alerts]
