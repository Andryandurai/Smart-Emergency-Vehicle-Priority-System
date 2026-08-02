"""Analytics computations behind features 4.8 and 4.9.

Everything is expressed in plain ORM aggregates plus a little Python, so the
same code runs on SQLite during a pilot and on PostgreSQL in production
without a second implementation.
"""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import median

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from apps.analytics.models import Hotspot
from apps.core.enums import PreemptionState, TripStage
from apps.core.geo import haversine_m


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[index]


# ---------------------------------------------------------------------------
# 4.8 - AI Traffic Analytics Dashboard
# ---------------------------------------------------------------------------
def response_time_stats(days: int = 30) -> dict:
    """Average / median / p90 ambulance response and transport times."""
    from apps.dispatch.models import EmergencyTrip

    since = timezone.now() - timezone.timedelta(days=days)
    trips = EmergencyTrip.objects.filter(created_at__gte=since)

    response, transport, total = [], [], []
    for trip in trips.only(
        "dispatched_at", "arrived_scene_at", "departed_scene_at",
        "arrived_hospital_at", "handover_at",
    ):
        if trip.response_time_s is not None:
            response.append(trip.response_time_s)
        if trip.transport_time_s is not None:
            transport.append(trip.transport_time_s)
        if trip.total_time_s is not None:
            total.append(trip.total_time_s)

    def summarise(values: list[float]) -> dict:
        return {
            "count": len(values),
            "avg_s": round(sum(values) / len(values), 1) if values else None,
            "avg_min": round(sum(values) / len(values) / 60, 2) if values else None,
            "median_s": round(median(values), 1) if values else None,
            "p90_s": round(_percentile(values, 90), 1) if values else None,
            "best_s": round(min(values), 1) if values else None,
            "worst_s": round(max(values), 1) if values else None,
        }

    return {
        "window_days": days,
        "trips": trips.count(),
        "completed": trips.filter(
            stage__in=[TripStage.ARRIVED, TripStage.HANDOVER]
        ).count(),
        "cancelled": trips.filter(stage=TripStage.CANCELLED).count(),
        "response_time": summarise(response),
        "transport_time": summarise(transport),
        "total_time": summarise(total),
    }


def corridor_usage(days: int = 30) -> dict:
    """Green corridor usage and, importantly, its cost to other road users."""
    from apps.dispatch.models import SignalPreemption

    since = timezone.now() - timezone.timedelta(days=days)
    rows = SignalPreemption.objects.filter(created_at__gte=since)

    released = rows.filter(state=PreemptionState.RELEASED).exclude(
        activated_at__isnull=True
    ).exclude(released_at__isnull=True)

    hold_seconds, eta_errors = [], []
    for row in released.only("activated_at", "released_at", "predicted_arrival_at", "actual_arrival_at"):
        if row.actual_hold_s is not None:
            hold_seconds.append(row.actual_hold_s)
        if row.eta_error_s is not None:
            eta_errors.append(abs(row.eta_error_s))

    by_state = dict(rows.values_list("state").annotate(n=Count("id")))
    return {
        "window_days": days,
        "preemptions_requested": rows.count(),
        "by_state": by_state,
        "activated": rows.filter(activated_at__isnull=False).count(),
        "failed": rows.filter(state=PreemptionState.FAILED).count(),
        "yielded": rows.filter(yielded_to__isnull=False).count(),
        "total_hold_seconds": round(sum(hold_seconds), 1),
        "avg_hold_seconds": (
            round(sum(hold_seconds) / len(hold_seconds), 1) if hold_seconds else None
        ),
        "eta_accuracy": {
            "samples": len(eta_errors),
            "mean_abs_error_s": (
                round(sum(eta_errors) / len(eta_errors), 1) if eta_errors else None
            ),
            "p90_abs_error_s": round(_percentile(eta_errors, 90), 1) if eta_errors else None,
        },
        "trips_with_corridor": rows.values("trip_id").distinct().count(),
    }


def congestion_hotspots(days: int = 14, limit: int = 20) -> list[dict]:
    """Segments that are congested most consistently."""
    from apps.network.models import TrafficObservation

    since = timezone.now() - timezone.timedelta(days=days)
    rows = (
        TrafficObservation.objects.filter(observed_at__gte=since)
        .values("segment_id", "segment__name", "segment__latitude", "segment__longitude")
        .annotate(
            samples=Count("id"),
            avg_speed=Avg("speed_kmh"),
            heavy=Count("id", filter=Q(congestion_level__in=["heavy", "jam"])),
        )
        .filter(samples__gte=5)
    )

    results = []
    for row in rows:
        share = row["heavy"] / row["samples"]
        results.append(
            {
                "segment_id": row["segment_id"],
                "name": row["segment__name"] or f"Segment {row['segment_id']}",
                "latitude": row["segment__latitude"],
                "longitude": row["segment__longitude"],
                "samples": row["samples"],
                "avg_speed_kmh": round(row["avg_speed"], 1),
                "heavy_share": round(share, 3),
                "score": round(share, 3),
            }
        )
    results.sort(key=lambda r: -r["score"])
    return results[:limit]


def high_delay_intersections(days: int = 30, limit: int = 20) -> list[dict]:
    """Junctions where emergency vehicles lose the most time.

    Measured two ways and combined: how long corridors have to be held there
    (a proxy for queue length), and how far arrival predictions drift there
    (a proxy for unpredictable delay).
    """
    from apps.dispatch.models import SignalPreemption

    since = timezone.now() - timezone.timedelta(days=days)
    rows = (
        SignalPreemption.objects.filter(created_at__gte=since, activated_at__isnull=False)
        .values(
            "signal__intersection_id",
            "signal__intersection__name",
            "signal__intersection__latitude",
            "signal__intersection__longitude",
            "signal__controller_id",
        )
        .annotate(events=Count("id"), total_hold=Sum("hold_duration_s"), avg_clear=Avg("clearance_s"))
        .filter(events__gte=2)
    )

    enriched = []
    for row in rows:
        preemptions = SignalPreemption.objects.filter(
            signal__intersection_id=row["signal__intersection_id"],
            created_at__gte=since,
            predicted_arrival_at__isnull=False,
            actual_arrival_at__isnull=False,
        ).only("predicted_arrival_at", "actual_arrival_at")
        errors = [abs(p.eta_error_s) for p in preemptions if p.eta_error_s is not None]
        avg_error = sum(errors) / len(errors) if errors else 0.0

        enriched.append(
            {
                "intersection_id": row["signal__intersection_id"],
                "name": row["signal__intersection__name"] or f"Node {row['signal__intersection_id']}",
                "controller_id": row["signal__controller_id"],
                "latitude": row["signal__intersection__latitude"],
                "longitude": row["signal__intersection__longitude"],
                "events": row["events"],
                "avg_hold_s": round((row["total_hold"] or 0) / row["events"], 1),
                "avg_clearance_s": round(row["avg_clear"] or 0, 1),
                "avg_eta_error_s": round(avg_error, 1),
                # Clearance time dominates: it directly measures queue length.
                "score": round((row["avg_clear"] or 0) / 35.0 + avg_error / 60.0, 3),
            }
        )
    enriched.sort(key=lambda r: -r["score"])
    return enriched[:limit]


def emergency_movement_stats(days: int = 30) -> dict:
    """Fleet activity summary for the traffic authority."""
    from apps.dispatch.models import EmergencyTrip, RoutePlan
    from apps.fleet.models import EmergencyVehicle, VehicleTelemetry

    since = timezone.now() - timezone.timedelta(days=days)
    trips = EmergencyTrip.objects.filter(created_at__gte=since)
    plans = RoutePlan.objects.filter(computed_at__gte=since)

    by_category = dict(
        trips.values_list("emergency_category").annotate(n=Count("id"))
    )
    by_priority = dict(trips.values_list("priority_level").annotate(n=Count("id")))

    distance = plans.filter(is_active=True).aggregate(total=Sum("total_distance_m"))["total"] or 0
    return {
        "window_days": days,
        "trips": trips.count(),
        "trips_by_category": by_category,
        "trips_by_priority_level": by_priority,
        "reroutes": max(0, plans.count() - trips.count()),
        "planned_distance_km": round(distance / 1000.0, 1),
        "telemetry_points": VehicleTelemetry.objects.filter(recorded_at__gte=since).count(),
        "fleet": {
            "total": EmergencyVehicle.objects.count(),
            "online": EmergencyVehicle.objects.online().count(),
            "on_mission": EmergencyVehicle.objects.on_mission().count(),
            "available": EmergencyVehicle.objects.deployable().count(),
        },
        "hospital_overrides": trips.filter(hospital_was_overridden=True).count(),
    }


# ---------------------------------------------------------------------------
# 4.9 - Accident hotspot identification
# ---------------------------------------------------------------------------
def identify_accident_hotspots(
    *, window_days: int = 180, cell_m: float = 250.0, min_incidents: int = 3, persist: bool = True
) -> list[dict]:
    """Grid-cluster historical accidents into actionable hotspots.

    A fixed grid is used rather than DBSCAN so the result is stable between
    runs - an authority planning junction improvements needs the same hotspot
    to keep the same identity month to month, and a density-based clusterer
    reshuffles boundaries whenever a single point is added.
    """
    from apps.network.models import AccidentRecord, Intersection

    since = timezone.now() - timezone.timedelta(days=window_days)
    records = list(
        AccidentRecord.objects.filter(occurred_at__gte=since).only(
            "latitude", "longitude", "severity", "casualties"
        )
    )
    if not records:
        return []

    # Degrees per cell, derived at the data's own latitude.
    mean_lat = sum(r.latitude for r in records) / len(records)
    dlat = cell_m / 111_320.0
    dlon = cell_m / (111_320.0 * max(0.1, math.cos(math.radians(mean_lat))))

    buckets: dict[tuple[int, int], list] = defaultdict(list)
    for record in records:
        buckets[(int(record.latitude / dlat), int(record.longitude / dlon))].append(record)

    clusters = []
    for members in buckets.values():
        if len(members) < min_incidents:
            continue
        lat = sum(m.latitude for m in members) / len(members)
        lon = sum(m.longitude for m in members) / len(members)
        severity_sum = sum(m.severity for m in members)
        casualties = sum(m.casualties for m in members)
        clusters.append(
            {
                "latitude": lat,
                "longitude": lon,
                "incident_count": len(members),
                "severity_sum": severity_sum,
                "casualties": casualties,
                "raw_score": severity_sum + 0.5 * casualties,
            }
        )

    if not clusters:
        return []

    worst = max(c["raw_score"] for c in clusters)
    for cluster in clusters:
        cluster["score"] = round(cluster["raw_score"] / worst, 3)
    clusters.sort(key=lambda c: -c["score"])

    # Attach the nearest intersection so the finding is actionable.
    intersections = list(Intersection.objects.only("id", "name", "latitude", "longitude"))
    for cluster in clusters:
        nearest, best = None, float("inf")
        for node in intersections:
            d = haversine_m(cluster["latitude"], cluster["longitude"], node.latitude, node.longitude)
            if d < best:
                nearest, best = node, d
        if nearest is not None and best <= 400:
            cluster["intersection_id"] = nearest.id
            cluster["label"] = nearest.label
            cluster["distance_to_intersection_m"] = round(best, 1)
        else:
            cluster["intersection_id"] = None
            cluster["label"] = f"{cluster['latitude']:.4f}, {cluster['longitude']:.4f}"

    if persist:
        Hotspot.objects.filter(kind=Hotspot.Kind.ACCIDENT).delete()
        Hotspot.objects.bulk_create(
            [
                Hotspot(
                    kind=Hotspot.Kind.ACCIDENT,
                    label=c["label"],
                    latitude=c["latitude"],
                    longitude=c["longitude"],
                    intersection_id=c["intersection_id"],
                    incident_count=c["incident_count"],
                    score=c["score"],
                    window_days=window_days,
                    details={
                        "severity_sum": c["severity_sum"],
                        "casualties": c["casualties"],
                        "cell_m": cell_m,
                    },
                )
                for c in clusters
            ]
        )

    return clusters


def rollup_daily_metrics(date=None, city: str = "Chennai") -> "DailyMetric":
    """Materialise one day's metrics. Idempotent - safe to re-run."""
    from apps.alerts.models import DriverAlert
    from apps.analytics.models import DailyMetric
    from apps.dispatch.models import EmergencyTrip, RoutePlan, SignalPreemption

    day = date or timezone.localdate()
    start = timezone.make_aware(
        timezone.datetime.combine(day, timezone.datetime.min.time())
    )
    end = start + timezone.timedelta(days=1)

    trips = EmergencyTrip.objects.filter(created_at__gte=start, created_at__lt=end)
    preemptions = SignalPreemption.objects.filter(created_at__gte=start, created_at__lt=end)
    plans = RoutePlan.objects.filter(computed_at__gte=start, computed_at__lt=end)

    response, transport = [], []
    for trip in trips:
        if trip.response_time_s is not None:
            response.append(trip.response_time_s)
        if trip.transport_time_s is not None:
            transport.append(trip.transport_time_s)

    eta_errors = [
        abs(p.eta_error_s)
        for p in preemptions.filter(
            predicted_arrival_at__isnull=False, actual_arrival_at__isnull=False
        )
        if p.eta_error_s is not None
    ]
    holds = [p.actual_hold_s for p in preemptions if p.actual_hold_s is not None]

    metric, _ = DailyMetric.objects.update_or_create(
        date=day,
        city=city,
        defaults={
            "trips_total": trips.count(),
            "trips_completed": trips.filter(
                stage__in=[TripStage.ARRIVED, TripStage.HANDOVER]
            ).count(),
            "trips_cancelled": trips.filter(stage=TripStage.CANCELLED).count(),
            "avg_response_time_s": (sum(response) / len(response)) if response else None,
            "median_response_time_s": median(response) if response else None,
            "p90_response_time_s": _percentile(response, 90),
            "avg_transport_time_s": (sum(transport) / len(transport)) if transport else None,
            "green_corridors_created": preemptions.values("trip_id").distinct().count(),
            "signals_preempted": preemptions.filter(activated_at__isnull=False).count(),
            "total_hold_seconds": sum(holds),
            "avg_eta_error_s": (sum(eta_errors) / len(eta_errors)) if eta_errors else None,
            "reroutes": max(0, plans.count() - trips.count()),
            "driver_alerts_issued": DriverAlert.objects.filter(
                created_at__gte=start, created_at__lt=end
            ).count(),
            "hospital_overrides": trips.filter(hospital_was_overridden=True).count(),
        },
    )
    return metric


def dashboard_summary(days: int = 30) -> dict:
    """One call powering the whole AI Traffic Analytics Dashboard."""
    return {
        "generated_at": timezone.now(),
        "response_times": response_time_stats(days),
        "corridor_usage": corridor_usage(days),
        "congestion_hotspots": congestion_hotspots(min(days, 14)),
        "high_delay_intersections": high_delay_intersections(days),
        "movement": emergency_movement_stats(days),
    }
