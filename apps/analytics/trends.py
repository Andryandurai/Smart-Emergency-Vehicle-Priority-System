"""Chart-shaped analytics: time series, distributions and profiles.

Phase 10. The existing :mod:`apps.analytics.services` answers "what were the
numbers over the last 30 days" — one aggregate per metric. A chart needs the
other thing: **the same metric, day by day**, so a controller can see whether
response times are improving or whether last Tuesday was an outlier.

Three properties everything here maintains, because getting any of them wrong
produces a chart that is confidently misleading:

**1. Series are contiguous.**
Every day in the window is emitted, including days with no activity. A series
that simply omits quiet days compresses the x-axis, and a fortnight with two
busy days renders identically to a fortnight that was busy throughout.

**2. Zero and "not measured" are different values.**
``trips: 0`` is a real measurement — nothing happened. ``avg_response_s: null``
means nothing was measurable. Plotting the second as zero draws a cliff to the
floor that reads as a dramatic improvement in ambulance response times. So
counts default to 0 and measurements default to ``None``, and the frontend is
told which is which.

**3. Buckets are labelled with their boundaries, not their index.**
An hourly profile keyed 0..23 is ambiguous about timezone; these carry an
explicit local-time label, because "the 3am peak" is a claim about Chennai
local time, not UTC.

Source of truth
---------------
Historical days come from :class:`~apps.analytics.models.DailyMetric` where a
rollup exists, and are computed live otherwise. That combination is deliberate:
rollups are the materialised record and are cheap to read across 90 days, but
today's rollup has not been run yet, and an analytics dashboard whose most
recent point is always missing is one nobody trusts. The response reports which
days were materialised, so a gap in the rollup schedule is visible rather than
silently smoothed over.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import median

from django.utils import timezone

from apps.core.enums import PreemptionState, TripStage

#: Longest window a single request may ask for. Ninety daily points is already
#: more than a chart can render legibly; beyond that the cost is a full table
#: scan per request for a picture nobody can read.
MAX_WINDOW_DAYS = 365
DEFAULT_WINDOW_DAYS = 30


@dataclass(frozen=True)
class SeriesSpec:
    """Chart metadata for one metric, so the client does not hard-code it.

    Colour and axis live here rather than in the React component for the same
    reason the GIS layer registry lives on the server: adding a metric should
    be one change, and two screens plotting "response time" should not be able
    to disagree about what it is measured in.
    """

    key: str
    label: str
    unit: str
    #: "count" -> missing days are 0. "measure" -> missing days are null.
    kind: str
    colour: str
    #: Whether a rising line is good news, and therefore what colour the trend
    #: arrow gets. Three states, not two:
    #:
    #:   True  - up is an improvement (completions)
    #:   False - up is a regression   (response time, cross-traffic held)
    #:   None  - **neither**. Demand metrics belong here: a city having more
    #:           emergencies this fortnight is not SEVPS performing worse, and
    #:           painting it red says it is. A dashboard that cries wolf about
    #:           things nobody controls gets ignored about the things they do.
    higher_is_better: bool | None = None
    description: str = ""


SERIES: dict[str, SeriesSpec] = {
    "trips_total": SeriesSpec(
        "trips_total", "Emergency trips", "trips", "count", "#4da3ff",
        description="Responses opened that day.",
    ),
    "trips_completed": SeriesSpec(
        "trips_completed", "Completed", "trips", "count", "#2ecc71", True,
        description="Trips that reached hospital arrival or handover.",
    ),
    "avg_response_time_s": SeriesSpec(
        "avg_response_time_s", "Avg response time", "s", "measure", "#ff9f43",
        higher_is_better=False,
        description="Dispatch to scene arrival. The headline ambulance metric.",
    ),
    "median_response_time_s": SeriesSpec(
        "median_response_time_s", "Median response time", "s", "measure", "#ffd166",
        higher_is_better=False,
        description="Less sensitive to one very long call than the mean.",
    ),
    "p90_response_time_s": SeriesSpec(
        "p90_response_time_s", "p90 response time", "s", "measure", "#e74c3c",
        higher_is_better=False,
        description="The slow tail - what the worst-served 10% experienced.",
    ),
    "avg_transport_time_s": SeriesSpec(
        "avg_transport_time_s", "Avg transport time", "s", "measure", "#9b8cff",
        higher_is_better=False,
        description="Scene departure to hospital arrival.",
    ),
    "green_corridors_created": SeriesSpec(
        "green_corridors_created", "Green corridors", "corridors", "count", "#2ecc71",
        description="Trips that received signal priority.",
    ),
    "signals_preempted": SeriesSpec(
        "signals_preempted", "Signals held", "signals", "count", "#00d1b2",
        description="Junctions actually switched to green.",
    ),
    "total_hold_seconds": SeriesSpec(
        "total_hold_seconds", "Cross-traffic held", "s", "count", "#ff6b6b",
        higher_is_better=False,
        description="Total seconds other road users waited. The cost side of the corridor.",
    ),
    "avg_eta_error_s": SeriesSpec(
        "avg_eta_error_s", "Mean ETA error", "s", "measure", "#c77dff",
        higher_is_better=False,
        description="How wrong the arrival prediction was. Drives preemption timing.",
    ),
    "reroutes": SeriesSpec(
        "reroutes", "Dynamic reroutes", "reroutes", "count", "#f39c12",
        higher_is_better=False,
        description="Routes recomputed mid-trip (feature 4.10).",
    ),
    "driver_alerts_issued": SeriesSpec(
        "driver_alerts_issued", "Driver alerts", "alerts", "count", "#4da3ff",
        description="Advance warnings issued to road users (Layer 4).",
    ),
    "hospital_overrides": SeriesSpec(
        "hospital_overrides", "Crew overrides", "trips", "count", "#ff9f43",
        higher_is_better=False,
        description="Crew chose a different hospital than recommended.",
    ),
}

#: What the dashboard charts by default. Everything else stays available.
DEFAULT_SERIES = (
    "trips_total",
    "avg_response_time_s",
    "green_corridors_created",
    "total_hold_seconds",
)


def _window(days: int) -> tuple:
    days = max(1, min(int(days), MAX_WINDOW_DAYS))
    today = timezone.localdate()
    start = today - timezone.timedelta(days=days - 1)
    return start, today, days


def _day_bounds(day):
    start = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()))
    return start, start + timezone.timedelta(days=1)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[index]


def _round(value, digits: int = 1):
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------------------
# Daily time series
# ---------------------------------------------------------------------------
def _live_day(day, city: str) -> dict:
    """Compute one day's metrics without writing a rollup row.

    Same arithmetic as :func:`services.rollup_daily_metrics`, but read-only:
    a dashboard request must never have the side effect of materialising a
    partial day, or the rollup command would later find the row already there
    and skip the complete recompute.
    """
    from apps.alerts.models import DriverAlert
    from apps.dispatch.models import EmergencyTrip, RoutePlan, SignalPreemption

    start, end = _day_bounds(day)
    trips = EmergencyTrip.objects.filter(created_at__gte=start, created_at__lt=end)
    preemptions = SignalPreemption.objects.filter(created_at__gte=start, created_at__lt=end)
    plans = RoutePlan.objects.filter(computed_at__gte=start, computed_at__lt=end)

    response, transport = [], []
    for trip in trips:
        if trip.response_time_s is not None:
            response.append(trip.response_time_s)
        if trip.transport_time_s is not None:
            transport.append(trip.transport_time_s)

    errors = [
        abs(p.eta_error_s)
        for p in preemptions.filter(
            predicted_arrival_at__isnull=False, actual_arrival_at__isnull=False
        )
        if p.eta_error_s is not None
    ]
    holds = [p.actual_hold_s for p in preemptions if p.actual_hold_s is not None]
    trip_count = trips.count()

    return {
        "trips_total": trip_count,
        "trips_completed": trips.filter(
            stage__in=[TripStage.ARRIVED, TripStage.HANDOVER]
        ).count(),
        "trips_cancelled": trips.filter(stage=TripStage.CANCELLED).count(),
        "avg_response_time_s": _round(sum(response) / len(response)) if response else None,
        "median_response_time_s": _round(median(response)) if response else None,
        "p90_response_time_s": _round(_percentile(response, 90)),
        "avg_transport_time_s": _round(sum(transport) / len(transport)) if transport else None,
        "green_corridors_created": preemptions.values("trip_id").distinct().count(),
        "signals_preempted": preemptions.filter(activated_at__isnull=False).count(),
        "total_hold_seconds": _round(sum(holds)),
        "avg_eta_error_s": _round(sum(errors) / len(errors)) if errors else None,
        "reroutes": max(0, plans.count() - trip_count),
        "driver_alerts_issued": DriverAlert.objects.filter(
            created_at__gte=start, created_at__lt=end
        ).count(),
        "hospital_overrides": trips.filter(hospital_was_overridden=True).count(),
    }


def _from_metric(metric) -> dict:
    return {
        "trips_total": metric.trips_total,
        "trips_completed": metric.trips_completed,
        "trips_cancelled": metric.trips_cancelled,
        "avg_response_time_s": _round(metric.avg_response_time_s),
        "median_response_time_s": _round(metric.median_response_time_s),
        "p90_response_time_s": _round(metric.p90_response_time_s),
        "avg_transport_time_s": _round(metric.avg_transport_time_s),
        "green_corridors_created": metric.green_corridors_created,
        "signals_preempted": metric.signals_preempted,
        "total_hold_seconds": _round(metric.total_hold_seconds),
        "avg_eta_error_s": _round(metric.avg_eta_error_s),
        "reroutes": metric.reroutes,
        "driver_alerts_issued": metric.driver_alerts_issued,
        "hospital_overrides": metric.hospital_overrides,
    }


def daily_series(days: int = DEFAULT_WINDOW_DAYS, city: str = "Chennai") -> dict:
    """One point per day across the window, materialised rows preferred."""
    from apps.analytics.models import DailyMetric

    start, today, days = _window(days)
    rolled = {
        metric.date: metric
        for metric in DailyMetric.objects.filter(date__gte=start, date__lte=today, city=city)
    }

    points, materialised = [], 0
    for offset in range(days):
        day = start + timezone.timedelta(days=offset)
        metric = rolled.get(day)
        if metric is not None:
            values = _from_metric(metric)
            materialised += 1
        else:
            values = _live_day(day, city)
        points.append({"date": day.isoformat(), "label": day.strftime("%d %b"), **values})

    return {
        "window_days": days,
        "start": start.isoformat(),
        "end": today.isoformat(),
        "city": city,
        "points": points,
        "series": [
            {
                "key": spec.key, "label": spec.label, "unit": spec.unit,
                "kind": spec.kind, "colour": spec.colour,
                "higher_is_better": spec.higher_is_better,
                "description": spec.description,
            }
            for spec in SERIES.values()
        ],
        "default_series": list(DEFAULT_SERIES),
        # Visible on purpose: a dashboard silently computing 90 days live is a
        # missing rollup schedule, not a feature.
        "materialised_days": materialised,
        "computed_live_days": days - materialised,
    }


def trend(days: int = DEFAULT_WINDOW_DAYS, city: str = "Chennai") -> dict:
    """Each metric's current half compared with its previous half.

    Deliberately not "today vs yesterday": emergency volume is noisy enough
    day to day that such a comparison produces a red arrow roughly half the
    time regardless of what is happening. Splitting the window in two is the
    smallest comparison that means something.
    """
    series = daily_series(days, city)
    points = series["points"]
    if len(points) < 4:
        return {"window_days": series["window_days"], "comparable": False, "metrics": []}

    half = len(points) // 2
    older, newer = points[:half], points[half:]

    metrics = []
    for spec in SERIES.values():
        before = _aggregate(older, spec)
        after = _aggregate(newer, spec)
        if before is None or after is None:
            change = None
        elif before == 0:
            change = None if after == 0 else 100.0
        else:
            change = round((after - before) / abs(before) * 100, 1)

        # None stays None for a neutral metric: there is no such thing as
        # emergency volume "improving", and forcing a verdict onto it is how a
        # dashboard ends up alarming about the weather.
        improving = None
        if change not in (None, 0) and spec.higher_is_better is not None:
            improving = (change > 0) == spec.higher_is_better

        metrics.append(
            {
                "key": spec.key, "label": spec.label, "unit": spec.unit,
                "previous": before, "current": after,
                "change_pct": change, "improving": improving,
                "higher_is_better": spec.higher_is_better,
            }
        )

    return {
        "window_days": series["window_days"],
        "comparable": True,
        "split_at": newer[0]["date"],
        "metrics": metrics,
    }


def _aggregate(points: list[dict], spec: SeriesSpec):
    """Counts sum; measurements average over the days that had one."""
    values = [p[spec.key] for p in points if p.get(spec.key) is not None]
    if not values:
        return None
    if spec.kind == "count":
        return round(sum(values), 1)
    return round(sum(values) / len(values), 1)


# ---------------------------------------------------------------------------
# Demand profile
# ---------------------------------------------------------------------------
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def demand_profile(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """When emergencies happen: by hour of day and by day of week.

    This is the analysis that changes decisions — crew rostering and where to
    pre-position vehicles. Both axes are in **local time**, because a claim
    about the evening peak is a claim about Chennai, and a UTC-keyed profile
    shifts every peak by five and a half hours.
    """
    from apps.dispatch.models import EmergencyTrip

    start, today, days = _window(days)
    since, _ = _day_bounds(start)

    by_hour: Counter[int] = Counter()
    by_weekday: Counter[int] = Counter()
    by_hour_priority: dict[int, Counter] = defaultdict(Counter)
    response_by_hour: dict[int, list[float]] = defaultdict(list)

    trips = EmergencyTrip.objects.filter(created_at__gte=since).only(
        "created_at", "priority_level", "dispatched_at", "arrived_scene_at"
    )
    for trip in trips:
        local = timezone.localtime(trip.created_at)
        by_hour[local.hour] += 1
        by_weekday[local.weekday()] += 1
        by_hour_priority[local.hour][trip.priority_level] += 1
        if trip.response_time_s is not None:
            response_by_hour[local.hour].append(trip.response_time_s)

    total = sum(by_hour.values())
    hours = []
    for hour in range(24):
        samples = response_by_hour[hour]
        hours.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "trips": by_hour[hour],
                "share": round(by_hour[hour] / total, 4) if total else 0.0,
                # Correlating volume with response time is the point: a busy
                # hour that stays fast is capacity working, a busy hour that
                # slows down is capacity running out.
                "avg_response_s": _round(sum(samples) / len(samples)) if samples else None,
                "level_1": by_hour_priority[hour][1],
            }
        )

    weekdays = [
        {
            "weekday": index,
            "label": name,
            "trips": by_weekday[index],
            "share": round(by_weekday[index] / total, 4) if total else 0.0,
        }
        for index, name in enumerate(DAY_NAMES)
    ]

    peak = max(hours, key=lambda h: h["trips"]) if total else None
    return {
        "window_days": days,
        "trips": total,
        "hours": hours,
        "weekdays": weekdays,
        "peak_hour": peak["label"] if peak and peak["trips"] else None,
        "timezone": str(timezone.get_current_timezone()),
    }


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------
def category_distribution(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Trips by emergency category and by priority level, chart-ready.

    Returned as an ordered list of ``{key, label, value, colour}`` rather than
    a mapping, because a chart needs a stable order and a colour, and deriving
    either from dict iteration puts the same category in a different colour on
    two screens.
    """
    from apps.core.enums import EmergencyCategory, PriorityLevel
    from apps.dispatch.models import EmergencyTrip

    start, _, days = _window(days)
    since, _ = _day_bounds(start)
    trips = EmergencyTrip.objects.filter(created_at__gte=since)

    category_counts = Counter(trips.values_list("emergency_category", flat=True))
    level_counts = Counter(trips.values_list("priority_level", flat=True))

    categories = [
        {
            "key": value,
            "label": label,
            "value": category_counts.get(value, 0),
            "colour": CATEGORY_COLOURS.get(value, "#7f8c9b"),
        }
        for value, label in EmergencyCategory.choices
        # Zero-count categories are dropped from a pie (a zero slice is a
        # legend entry with no wedge) but kept in the total below.
        if category_counts.get(value, 0) > 0
    ]

    levels = [
        {
            "key": str(value),
            "label": f"Level {value} - {label}",
            "value": level_counts.get(value, 0),
            "colour": LEVEL_COLOURS.get(value, "#7f8c9b"),
        }
        for value, label in PriorityLevel.choices
    ]

    return {
        "window_days": days,
        "total": trips.count(),
        "categories": categories,
        "levels": levels,
    }


#: Fixed so a category is the same colour on every screen and in every export.
CATEGORY_COLOURS = {
    "cardiac": "#ff4d4f",
    "stroke": "#c77dff",
    "burn": "#f39c12",
    "trauma": "#ff9f43",
    "poisoning": "#9b8cff",
    "respiratory": "#00d1b2",
    "obstetric": "#4dd4c0",
    "pediatric": "#4da3ff",
    "fire_rescue": "#e74c3c",
    "transfer": "#7f8c9b",
    "unknown": "#5a6673",
}

LEVEL_COLOURS = {1: "#ff4d4f", 2: "#ff9f43", 3: "#ffd166", 4: "#7f8c9b"}


def corridor_outcomes(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Preemption outcomes per day - a stacked bar of what the corridor did.

    Activated / yielded / failed rather than one "corridors" count, because
    the three mean completely different things operationally: yielded is the
    system working correctly under contention, failed is a controller that did
    not answer, and only the second needs someone to go and look at a junction.
    """
    from apps.dispatch.models import SignalPreemption

    start, today, days = _window(days)
    since, _ = _day_bounds(start)

    buckets: dict[str, Counter] = defaultdict(Counter)
    rows = SignalPreemption.objects.filter(created_at__gte=since).only(
        "created_at", "state", "activated_at", "yielded_to_id"
    )
    for row in rows:
        day = timezone.localtime(row.created_at).date().isoformat()
        # Yielding is recorded by a relation, not a state - a preemption that
        # stood down for a higher-priority vehicle keeps whatever state it
        # reached. Checking `state` alone would file it as "cancelled", which
        # reads as a fault when it is the contention rule working correctly.
        if row.yielded_to_id is not None:
            buckets[day]["yielded"] += 1
        elif row.state == PreemptionState.FAILED:
            buckets[day]["failed"] += 1
        elif row.activated_at is not None:
            buckets[day]["activated"] += 1
        elif row.state == PreemptionState.CANCELLED:
            buckets[day]["cancelled"] += 1
        else:
            buckets[day]["pending"] += 1

    points = []
    for offset in range(days):
        day = start + timezone.timedelta(days=offset)
        counts = buckets.get(day.isoformat(), Counter())
        points.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%d %b"),
                "activated": counts["activated"],
                "yielded": counts["yielded"],
                "failed": counts["failed"],
                "cancelled": counts["cancelled"],
                "pending": counts["pending"],
            }
        )

    return {
        "window_days": days,
        "points": points,
        "legend": [
            {"key": "activated", "label": "Activated", "colour": "#2ecc71"},
            {"key": "yielded", "label": "Yielded to higher priority", "colour": "#ffd166"},
            {"key": "failed", "label": "Controller failed", "colour": "#ff4d4f"},
            {"key": "cancelled", "label": "Cancelled", "colour": "#7f8c9b"},
            {"key": "pending", "label": "Planned / armed", "colour": "#4da3ff"},
        ],
    }


#: Response-time histogram edges, in minutes. Chosen against the clinical
#: reality rather than round numbers: under 8 minutes is the threshold most
#: cardiac-arrest survival guidance is written around, so the buckets are dense
#: where the decision is and coarse in the tail.
RESPONSE_BUCKETS_MIN = (0, 4, 6, 8, 10, 15, 20, 30)


def response_distribution(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Histogram of response times.

    An average hides the shape. Two services with an eight-minute mean — one
    tightly clustered, one half at four minutes and half at twelve — are not
    the same service, and only the histogram shows that.
    """
    from apps.dispatch.models import EmergencyTrip

    start, _, days = _window(days)
    since, _ = _day_bounds(start)

    values = [
        trip.response_time_s
        for trip in EmergencyTrip.objects.filter(created_at__gte=since).only(
            "dispatched_at", "arrived_scene_at"
        )
        if trip.response_time_s is not None
    ]

    edges = RESPONSE_BUCKETS_MIN
    buckets = []
    for index, low in enumerate(edges):
        high = edges[index + 1] if index + 1 < len(edges) else None
        count = sum(
            1 for value in values
            if value >= low * 60 and (high is None or value < high * 60)
        )
        buckets.append(
            {
                "label": f"{low}-{high} min" if high else f"{low}+ min",
                "low_min": low,
                "high_min": high,
                "count": count,
                "share": round(count / len(values), 4) if values else 0.0,
                # The 8-minute line is drawn on the chart, so the threshold is
                # visible rather than something the reader has to know.
                "within_target": high is not None and high <= 8,
            }
        )

    within_target = sum(1 for value in values if value <= 8 * 60)
    return {
        "window_days": days,
        "samples": len(values),
        "buckets": buckets,
        "target_minutes": 8,
        "within_target": within_target,
        "within_target_share": round(within_target / len(values), 4) if values else None,
        "median_s": _round(median(values)) if values else None,
        "p90_s": _round(_percentile(values, 90)),
    }


def hospital_load(days: int = DEFAULT_WINDOW_DAYS, limit: int = 12) -> dict:
    """Which hospitals received the emergencies, and how the routing performed."""
    from apps.dispatch.models import EmergencyTrip

    start, _, days = _window(days)
    since, _ = _day_bounds(start)

    trips = (
        EmergencyTrip.objects.filter(created_at__gte=since, destination_hospital__isnull=False)
        .select_related("destination_hospital")
        .only(
            "destination_hospital__name", "destination_hospital__code",
            "hospital_was_overridden", "departed_scene_at", "arrived_hospital_at",
        )
    )

    counts: Counter[tuple] = Counter()
    overrides: Counter[tuple] = Counter()
    transports: dict[tuple, list[float]] = defaultdict(list)
    for trip in trips:
        key = (trip.destination_hospital.code, trip.destination_hospital.name)
        counts[key] += 1
        if trip.hospital_was_overridden:
            overrides[key] += 1
        if trip.transport_time_s is not None:
            transports[key].append(trip.transport_time_s)

    rows = []
    for (code, name), count in counts.most_common(limit):
        samples = transports[(code, name)]
        rows.append(
            {
                "code": code,
                "name": name,
                "trips": count,
                "overrides": overrides[(code, name)],
                # A high override rate on one hospital is the signal that the
                # recommender disagrees with the crews about that site.
                "override_share": round(overrides[(code, name)] / count, 3),
                "avg_transport_s": _round(sum(samples) / len(samples)) if samples else None,
            }
        )

    return {
        "window_days": days,
        "hospitals": rows,
        "total_routed": sum(counts.values()),
        "truncated": len(counts) > limit,
    }
