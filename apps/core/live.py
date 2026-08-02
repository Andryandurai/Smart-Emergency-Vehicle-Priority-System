"""Server-driven live updates that are not triggered by a GPS fix.

Most of SEVPS is reactive: a position arrives and the chain runs. Three things
change without any vehicle moving, and before this they only reached a screen
on the next telemetry tick - or never:

* **ETA** decays continuously. A vehicle stopped in traffic has a *worse* ETA
  every second, and that is exactly the moment a hospital most wants to know.
  Waiting for the next fix means the number a hospital reads is optimistic
  precisely when it matters.
* **Traffic** changes from camera analysis and provider feeds, which happen on
  the worker's schedule, not the fleet's.
* **Stale vehicles** stop reporting. Silence is information: a unit that has
  not reported for a minute may have lost signal mid-corridor.

Driven by ``manage.py sevps_worker``, so it runs in one place rather than
being duplicated per dashboard.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.core.realtime import broadcast, broadcast_ops, hospital_group, vehicle_group

log = logging.getLogger("sevps.live")

#: A vehicle silent for longer than this is announced as stale.
STALE_AFTER_S = 60
#: Only push an ETA that moved by more than this - a one-second drift on a
#: twelve-minute run is noise, and noise costs bandwidth on every dashboard.
ETA_CHANGE_THRESHOLD_S = 15.0

#: trip id -> last ETA pushed, so the threshold is measured against what the
#: client actually has rather than against the previous computation.
_last_eta: dict[int, float] = {}


def reset_state() -> None:
    """Forget push history - used by tests and on worker restart."""
    _last_eta.clear()


def tick_etas() -> dict:
    """Recompute ETA for every active trip and push material changes."""
    from apps.brain.eta import eta_for_trip
    from apps.dispatch.models import EmergencyTrip

    pushed = 0
    stalled = 0
    trips = list(
        EmergencyTrip.objects.active()
        .select_related("vehicle", "destination_hospital")
        .prefetch_related("routes")
    )

    live_ids = set()
    for trip in trips:
        live_ids.add(trip.id)
        payload = eta_for_trip(trip)
        if payload is None:
            continue

        remaining = payload["remaining_s"]
        previous = _last_eta.get(trip.id)
        if previous is not None and abs(previous - remaining) < ETA_CHANGE_THRESHOLD_S:
            continue
        _last_eta[trip.id] = remaining

        if payload.get("is_stalled"):
            stalled += 1

        event = {
            "trip_id": trip.id,
            "reference": trip.reference,
            "vehicle": trip.vehicle.callsign,
            "eta": payload["eta"],
            "remaining_s": remaining,
            "remaining_m": payload["remaining_m"],
            "completion": payload["completion"],
            "is_stalled": payload["is_stalled"],
            "is_on_route": payload["is_on_route"],
        }
        broadcast_ops("eta_update", event)
        broadcast(vehicle_group(trip.vehicle.callsign), "eta_update", event)
        if trip.destination_hospital_id:
            broadcast(hospital_group(trip.destination_hospital.code), "eta_update", event)

        # Persist so a dashboard opening later reads the same number.
        trip.eta = payload["eta"]
        trip.distance_remaining_m = payload["remaining_m"]
        trip.save(update_fields=["eta", "distance_remaining_m", "updated_at"])
        pushed += 1

    # A trip that ended should not keep an entry forever.
    for trip_id in set(_last_eta) - live_ids:
        _last_eta.pop(trip_id, None)

    return {"active_trips": len(trips), "eta_pushed": pushed, "stalled": stalled}


def tick_traffic(limit: int = 200) -> dict:
    """Publish segments whose congestion changed since the last sweep.

    Only the *changed* ones. Broadcasting the whole network every few seconds
    would swamp the channel layer to tell every client what it already knows.
    """
    from apps.network.models import RoadSegment

    since = timezone.now() - timezone.timedelta(seconds=30)
    changed = list(
        RoadSegment.objects.filter(speed_updated_at__gte=since)
        .only("id", "name", "congestion_level", "congestion_index",
              "current_speed_kmh", "is_open", "latitude", "longitude")[:limit]
    )
    if not changed:
        return {"segments_changed": 0}

    broadcast_ops(
        "traffic_update",
        {
            "segments": [
                {
                    "segment_id": segment.id,
                    "name": segment.name,
                    "congestion_level": segment.congestion_level,
                    "congestion_index": round(segment.congestion_index, 3),
                    "speed_kmh": segment.current_speed_kmh,
                    "is_open": segment.is_open,
                    "latitude": segment.latitude,
                    "longitude": segment.longitude,
                }
                for segment in changed
            ],
            "observed_at": timezone.now(),
        },
    )
    return {"segments_changed": len(changed)}


def tick_fleet_health() -> dict:
    """Announce vehicles that have gone silent.

    A unit that stops reporting mid-corridor is holding signals green for a
    position nobody can confirm. The corridor sweep releases the hold on its
    own timer; this makes the cause visible to the control room.
    """
    from apps.fleet.models import EmergencyVehicle

    stale = list(
        EmergencyVehicle.objects.on_mission().stale(STALE_AFTER_S).only(
            "id", "callsign", "last_seen_at", "status"
        )
    )
    if not stale:
        return {"stale_vehicles": 0}

    now = timezone.now()
    broadcast_ops(
        "fleet_health",
        {
            "stale": [
                {
                    "callsign": vehicle.callsign,
                    "status": vehicle.status,
                    "last_seen_at": vehicle.last_seen_at,
                    "silent_for_s": (
                        round((now - vehicle.last_seen_at).total_seconds())
                        if vehicle.last_seen_at
                        else None
                    ),
                }
                for vehicle in stale
            ]
        },
    )
    return {"stale_vehicles": len(stale)}


def tick_all() -> dict:
    """One live sweep. Called by the worker; safe to call by hand."""
    result: dict = {}
    for name, fn in (("eta", tick_etas), ("traffic", tick_traffic), ("fleet", tick_fleet_health)):
        try:
            result[name] = fn()
        except Exception:  # pragma: no cover - a live push must never stop the worker
            log.warning("live tick %s failed", name, exc_info=True)
            result[name] = {"error": True}
    return result
