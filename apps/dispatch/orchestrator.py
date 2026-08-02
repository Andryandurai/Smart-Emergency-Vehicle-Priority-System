"""The reactive spine of SEVPS.

One GPS fix arrives and the whole platform responds:

    position -> route progress -> ETA -> green corridor -> driver alerts
             -> hospital dashboard -> priority re-assessment -> reroute check

:func:`on_vehicle_position` is that chain.  Keeping it in one readable place
(rather than scattering it across signals handlers) means the end-to-end
behaviour of the system can be read, reasoned about and tested as a unit.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.core.enums import TripStage, VehicleStatus
from apps.core.geo import Point
from apps.core.realtime import broadcast, broadcast_ops, hospital_group, vehicle_group
from apps.dispatch.models import EmergencyTrip, RoutePlan

log = logging.getLogger("sevps.dispatch")

#: Within this distance the vehicle is treated as having reached its target.
ARRIVAL_RADIUS_M = 70.0


# ---------------------------------------------------------------------------
# Trip creation and destination assignment
# ---------------------------------------------------------------------------
@transaction.atomic
def create_trip(
    *,
    vehicle,
    incident_point: Point | None = None,
    emergency_category: str = "unknown",
    incident_address: str = "",
    caller_number: str = "",
    patient_age: int | None = None,
    patient_notes: str = "",
) -> EmergencyTrip:
    """Open a new emergency response and start moving the vehicle to the scene."""
    from apps.dispatch.siren import apply_priority

    trip = EmergencyTrip.objects.create(
        vehicle=vehicle,
        emergency_category=emergency_category,
        incident_latitude=incident_point.lat if incident_point else None,
        incident_longitude=incident_point.lon if incident_point else None,
        incident_address=incident_address,
        caller_number=caller_number,
        patient_age=patient_age,
        patient_notes=patient_notes,
        stage=TripStage.TO_SCENE if incident_point else TripStage.CREATED,
        dispatched_at=timezone.now(),
    )

    vehicle.status = VehicleStatus.DISPATCHED
    vehicle.save(update_fields=["status", "updated_at"])

    apply_priority(trip, trigger="trip created", force=True)

    if incident_point:
        plan_route_to(trip, incident_point, reason="initial dispatch to scene")

    broadcast_ops("trip_created", _trip_event_payload(trip))
    log.info("Trip %s created for %s", trip.reference, vehicle.callsign)
    return trip


def assign_hospital(
    trip,
    *,
    hospital=None,
    emergency_category: str | None = None,
    override_reason: str = "",
    recompute_route: bool = True,
) -> dict:
    """Run Layer 5 and commit the destination.

    When ``hospital`` is given it is treated as a crew override: the
    recommendation still runs and is logged, so the divergence is on record.
    """
    from apps.hospitals.models import HospitalRecommendationLog
    from apps.hospitals.recommender import recommend_hospital
    from apps.dispatch.siren import apply_priority

    if emergency_category:
        trip.emergency_category = emergency_category
        trip.save(update_fields=["emergency_category", "updated_at"])

    origin = trip.incident_point or trip.vehicle.point
    recommendation = recommend_hospital(
        origin, trip.emergency_category, priority_level=trip.priority_level
    )

    chosen = hospital or recommendation.recommended
    if chosen is None:
        log.error("No hospital could be selected for trip %s", trip.reference)
        from apps.core import notifications

        notifications.no_hospital_available(
            trip, recommendation.relaxation_note or "No hospital in range can accept."
        )
        return {"error": "no suitable hospital found", "recommendation": recommendation.as_dict()}

    HospitalRecommendationLog.objects.create(
        trip=trip,
        emergency_category=trip.emergency_category,
        recommended=recommendation.recommended,
        chosen=chosen,
        override_reason=override_reason,
        candidates=[c.as_dict() for c in recommendation.candidates],
        rule_snapshot=recommendation.rule.as_dict(),
    )

    trip.destination_hospital = chosen
    trip.destination_latitude = chosen.latitude
    trip.destination_longitude = chosen.longitude
    trip.hospital_was_overridden = bool(
        hospital and recommendation.recommended and hospital.id != recommendation.recommended.id
    )
    if trip.stage in {TripStage.CREATED, TripStage.TO_SCENE, TripStage.ON_SCENE}:
        trip.stage = TripStage.TO_HOSPITAL
        trip.departed_scene_at = trip.departed_scene_at or timezone.now()
    trip.save(
        update_fields=[
            "destination_hospital", "destination_latitude", "destination_longitude",
            "hospital_was_overridden", "stage", "departed_scene_at", "updated_at",
        ]
    )

    trip.vehicle.status = VehicleStatus.TRANSPORTING
    trip.vehicle.save(update_fields=["status", "updated_at"])

    # Severity is now known, so Layer 6 can set lights, siren and entitlement.
    apply_priority(trip, trigger="emergency category confirmed", force=True)

    if recompute_route:
        plan_route_to(
            trip,
            Point(chosen.latitude, chosen.longitude),
            reason=f"transport to {chosen.name}",
        )

    notify_hospital(trip, message="Inbound patient - pre-arrival notification")

    payload = {
        **_trip_event_payload(trip),
        "hospital": {"code": chosen.code, "name": chosen.name},
        "was_overridden": trip.hospital_was_overridden,
        "relaxed": recommendation.relaxed,
    }
    broadcast_ops("hospital_assigned", payload)
    return {"trip": payload, "recommendation": recommendation.as_dict()}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
def plan_route_to(trip, destination: Point, *, reason: str = "") -> RoutePlan | None:
    """Compute and commit a route from the vehicle's current position."""
    from apps.brain.router import RouteNotFound, route_between

    try:
        route = route_between(
            trip.vehicle.point,
            destination,
            priority_level=trip.priority_level,
            allow_contraflow=trip.allow_contraflow,
        )
    except RouteNotFound as exc:
        log.error("Route planning failed for trip %s: %s", trip.reference, exc)
        broadcast_ops(
            "route_failed", {"trip_id": trip.id, "reference": trip.reference, "detail": str(exc)}
        )
        return None

    trip.destination_latitude = destination.lat
    trip.destination_longitude = destination.lon
    trip.save(update_fields=["destination_latitude", "destination_longitude", "updated_at"])
    return apply_new_route(trip, route, reason=reason)


@transaction.atomic
def apply_new_route(trip, route, *, reason: str = "") -> RoutePlan:
    """Persist a computed route as the trip's active plan and re-arm Layer 3."""
    from apps.dispatch.corridor import sync_corridor

    trip.routes.filter(is_active=True).update(is_active=False)

    plan = RoutePlan.objects.create(
        trip=trip,
        algorithm=route.algorithm,
        reason=reason,
        origin_latitude=route.origin.lat,
        origin_longitude=route.origin.lon,
        destination_latitude=route.destination.lat,
        destination_longitude=route.destination.lon,
        geometry=route.geometry,
        steps=[s.as_dict() for s in route.steps],
        node_ids=route.nodes,
        total_distance_m=route.total_distance_m,
        total_duration_s=route.total_duration_s,
        computed_at=timezone.now(),
        predicted_eta=route.eta,
    )

    trip.eta = route.eta
    trip.distance_remaining_m = route.total_distance_m
    trip.save(update_fields=["eta", "distance_remaining_m", "updated_at"])

    payload = {
        "trip_id": trip.id,
        "reference": trip.reference,
        "vehicle": trip.vehicle.callsign,
        "route_plan_id": plan.id,
        "reason": reason,
        "algorithm": plan.algorithm,
        "duration_s": round(plan.total_duration_s, 1),
        "distance_m": round(plan.total_distance_m, 1),
        "eta": plan.predicted_eta,
        "geometry": plan.geometry,
        "signal_count": len(plan.signalised_steps),
    }
    broadcast_ops("route_updated", payload)
    broadcast(vehicle_group(trip.vehicle.callsign), "route_updated", payload)

    # Corridor must follow the route, not the other way round.
    transaction.on_commit(lambda: sync_corridor(trip))

    log.info(
        "Trip %s routed: %.1f km / %.1f min (%s)",
        trip.reference, plan.total_distance_m / 1000, plan.total_duration_s / 60, reason,
    )
    return plan


# ---------------------------------------------------------------------------
# The reactive chain
# ---------------------------------------------------------------------------
def on_vehicle_position(vehicle) -> dict | None:
    """Everything that must happen when a vehicle reports a new position."""
    from apps.alerts.dispatcher import broadcast_driver_alerts
    from apps.brain.eta import compute_progress, eta_for_trip
    from apps.brain.rerouting import evaluate_trip
    from apps.dispatch.corridor import sync_corridor
    from apps.dispatch.siren import apply_priority

    trip = vehicle.active_trip
    if trip is None:
        return None

    plan = trip.active_route
    outcome: dict = {"trip_id": trip.id, "reference": trip.reference, "stage": trip.stage}

    if plan is not None:
        progress = compute_progress(plan, vehicle.point)
        eta_payload = eta_for_trip(trip)
        if eta_payload:
            trip.eta = eta_payload["eta"]
            trip.distance_remaining_m = eta_payload["remaining_m"]
            trip.save(update_fields=["eta", "distance_remaining_m", "updated_at"])
            outcome["eta"] = eta_payload

        # Layer 3 - arm/activate/release green corridor signals.
        outcome["corridor"] = sync_corridor(trip, progress)

        # Layer 4 - warn drivers ahead of the vehicle.
        outcome["alerts"] = broadcast_driver_alerts(trip, progress)

        # Layer 2 - has the world changed enough to justify a new route?
        decision = evaluate_trip(trip, reason="position update")
        if decision.should_reroute and decision.route is not None:
            apply_new_route(trip, decision.route, reason=decision.reason)
            outcome["rerouted"] = decision.as_dict()

    # Layer 6 - re-derive lights/siren for the current stage and condition.
    directive = apply_priority(trip)
    if directive is not None:
        outcome["priority_directive"] = {
            "level": directive.priority_level,
            "siren_mode": directive.siren_mode,
            "trigger": directive.trigger,
        }

    # Automatic stage transitions on geofence arrival.
    transition = _check_arrival(trip)
    if transition:
        outcome["stage_change"] = transition

    if trip.destination_hospital_id:
        notify_hospital(trip, live_update=True)

    return outcome


def _check_arrival(trip) -> str | None:
    """Advance the trip stage when the vehicle reaches scene or hospital."""
    vehicle = trip.vehicle

    if trip.stage == TripStage.TO_SCENE and trip.incident_point:
        if vehicle.distance_to(trip.incident_point) <= ARRIVAL_RADIUS_M:
            advance_stage(trip, TripStage.ON_SCENE)
            return TripStage.ON_SCENE

    if trip.stage == TripStage.TO_HOSPITAL and trip.destination_point:
        if vehicle.distance_to(trip.destination_point) <= ARRIVAL_RADIUS_M:
            advance_stage(trip, TripStage.ARRIVED)
            return TripStage.ARRIVED

    return None


def advance_stage(trip, stage: str, *, reason: str = "") -> EmergencyTrip:
    """Move a trip to a new lifecycle stage and apply all side effects."""
    from apps.dispatch.corridor import release_corridor
    from apps.dispatch.siren import apply_priority, stand_down

    now = timezone.now()
    previous = trip.stage
    trip.stage = stage
    fields = ["stage", "updated_at"]

    if stage == TripStage.TO_SCENE and not trip.dispatched_at:
        trip.dispatched_at = now
        fields.append("dispatched_at")
    elif stage == TripStage.ON_SCENE:
        trip.arrived_scene_at = trip.arrived_scene_at or now
        trip.vehicle.status = VehicleStatus.ON_SCENE
        fields.append("arrived_scene_at")
    elif stage == TripStage.TO_HOSPITAL:
        trip.departed_scene_at = trip.departed_scene_at or now
        trip.vehicle.status = VehicleStatus.TRANSPORTING
        fields.append("departed_scene_at")
    elif stage == TripStage.ARRIVED:
        trip.arrived_hospital_at = trip.arrived_hospital_at or now
        trip.vehicle.status = VehicleStatus.AT_HOSPITAL
        fields.append("arrived_hospital_at")
    elif stage == TripStage.HANDOVER:
        trip.handover_at = trip.handover_at or now
        fields.append("handover_at")
    elif stage == TripStage.CANCELLED:
        trip.cancelled_at = now
        trip.cancellation_reason = reason
        fields += ["cancelled_at", "cancellation_reason"]

    trip.save(update_fields=fields)
    trip.vehicle.save(update_fields=["status", "updated_at"])

    if stage in {TripStage.ARRIVED, TripStage.HANDOVER, TripStage.CANCELLED}:
        release_corridor(trip, reason=f"trip {stage}")
        trip.routes.filter(is_active=True).update(is_active=False)
        if stage in {TripStage.HANDOVER, TripStage.CANCELLED}:
            stand_down(trip.vehicle, status=VehicleStatus.RETURNING)
        else:
            apply_priority(trip, trigger=f"stage -> {stage}")
    else:
        apply_priority(trip, trigger=f"stage -> {stage}")

    payload = {**_trip_event_payload(trip), "previous_stage": previous, "reason": reason}
    broadcast_ops("trip_stage", payload)
    broadcast(vehicle_group(trip.vehicle.callsign), "trip_stage", payload)
    if trip.destination_hospital_id:
        broadcast(hospital_group(trip.destination_hospital.code), "trip_stage", payload)

    log.info("Trip %s: %s -> %s %s", trip.reference, previous, stage, f"({reason})" if reason else "")
    return trip


def notify_hospital(trip, *, message: str = "", live_update: bool = False) -> dict | None:
    """Push the pre-arrival packet to the receiving hospital (Layer 5 / 4.7)."""
    from apps.hospitals.models import HospitalAlert
    from apps.hospitals.serializers import HospitalAlertSerializer

    hospital = trip.destination_hospital
    if hospital is None:
        return None

    payload = trip.as_hospital_payload()
    broadcast(hospital_group(hospital.code), "inbound_update", payload)

    if live_update:
        # Position tickers are streamed, not persisted - only state changes
        # and the initial notification create a durable alert record.
        return payload

    alert = HospitalAlert.objects.create(
        hospital=hospital,
        trip=trip,
        emergency_category=trip.emergency_category,
        priority_level=trip.priority_level,
        eta=trip.eta,
        distance_remaining_m=trip.distance_remaining_m,
        message=message or "Inbound emergency patient",
    )
    alert_payload = HospitalAlertSerializer(alert).data
    broadcast(hospital_group(hospital.code), "hospital_alert", alert_payload)
    broadcast_ops("hospital_alert", alert_payload)

    # A durable notification as well as the state event: a hospital screen
    # showing another patient still needs to know this one is coming.
    from apps.core import notifications

    notifications.hospital_prepare(trip, hospital)
    return alert_payload


def _trip_event_payload(trip) -> dict:
    return {
        "trip_id": trip.id,
        "reference": trip.reference,
        "uuid": str(trip.uuid),
        "vehicle": trip.vehicle.callsign,
        "stage": trip.stage,
        "stage_display": trip.get_stage_display(),
        "emergency_category": trip.emergency_category,
        "priority_level": trip.priority_level,
        "siren_mode": trip.siren_mode,
        "light_pattern": trip.light_pattern,
        "eta": trip.eta,
        "distance_remaining_m": trip.distance_remaining_m,
        "hospital": trip.destination_hospital.code if trip.destination_hospital_id else None,
        "latitude": trip.vehicle.latitude,
        "longitude": trip.vehicle.longitude,
    }
