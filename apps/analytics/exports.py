"""CSV export of the analytics datasets.

Cities run on spreadsheets. A dashboard that cannot produce a file to attach to
a quarterly report gets replaced by someone re-typing numbers out of it, and
re-typed numbers are wrong numbers. So every chart on the analytics screen has
a matching export of exactly the data it is drawn from — not a different query
that happens to be nearby, which is how a report and a dashboard end up
disagreeing.

Streamed rather than assembled: a year of daily metrics is small, but the trip
export is unbounded and building it in memory to hand to a browser is a
resident-set spike for no reason.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Callable, Iterator

from django.http import StreamingHttpResponse
from django.utils import timezone

from apps.analytics import trends


class _Echo:
    """A file-like object whose write() returns the line, for csv.writer."""

    def write(self, value):
        return value


@dataclass(frozen=True)
class Dataset:
    key: str
    title: str
    description: str
    headers: list[str]
    rows: Callable[[int], Iterator[list]]
    #: Whether the export contains operational detail beyond aggregate counts.
    #: Everything here is aggregate; the flag is here so a future per-trip
    #: export cannot be added without someone deciding what it exposes.
    aggregate_only: bool = True


def _daily_rows(days: int) -> Iterator[list]:
    data = trends.daily_series(days)
    keys = [spec.key for spec in trends.SERIES.values()]
    for point in data["points"]:
        yield [point["date"], *[point.get(key) for key in keys]]


def _demand_rows(days: int) -> Iterator[list]:
    data = trends.demand_profile(days)
    for hour in data["hours"]:
        yield [hour["label"], hour["trips"], hour["share"], hour["avg_response_s"], hour["level_1"]]


def _weekday_rows(days: int) -> Iterator[list]:
    for row in trends.demand_profile(days)["weekdays"]:
        yield [row["label"], row["trips"], row["share"]]


def _category_rows(days: int) -> Iterator[list]:
    data = trends.category_distribution(days)
    for row in data["categories"]:
        yield ["category", row["key"], row["label"], row["value"]]
    for row in data["levels"]:
        yield ["priority", row["key"], row["label"], row["value"]]


def _corridor_rows(days: int) -> Iterator[list]:
    for point in trends.corridor_outcomes(days)["points"]:
        yield [
            point["date"], point["activated"], point["yielded"],
            point["failed"], point["cancelled"], point["pending"],
        ]


def _response_rows(days: int) -> Iterator[list]:
    for bucket in trends.response_distribution(days)["buckets"]:
        yield [bucket["label"], bucket["count"], bucket["share"], bucket["within_target"]]


def _hospital_rows(days: int) -> Iterator[list]:
    for row in trends.hospital_load(days, limit=1000)["hospitals"]:
        yield [
            row["code"], row["name"], row["trips"], row["overrides"],
            row["override_share"], row["avg_transport_s"],
        ]


def _hotspot_rows(_days: int) -> Iterator[list]:
    from apps.analytics.models import Hotspot

    for spot in Hotspot.objects.all().order_by("-score"):
        yield [
            spot.kind, spot.label, spot.latitude, spot.longitude,
            spot.incident_count, round(spot.score, 4), spot.window_days,
        ]


DATASETS: dict[str, Dataset] = {
    "daily": Dataset(
        "daily", "Daily metrics",
        "One row per day: trips, response times, corridor usage and cost.",
        ["date", *[spec.key for spec in trends.SERIES.values()]],
        _daily_rows,
    ),
    "demand-hourly": Dataset(
        "demand-hourly", "Hourly demand profile",
        "Trips by hour of day, with the response time achieved in that hour.",
        ["hour", "trips", "share", "avg_response_s", "level_1_trips"],
        _demand_rows,
    ),
    "demand-weekday": Dataset(
        "demand-weekday", "Weekday demand profile",
        "Trips by day of week.",
        ["weekday", "trips", "share"],
        _weekday_rows,
    ),
    "categories": Dataset(
        "categories", "Emergency mix",
        "Trip counts by emergency category and by priority level.",
        ["dimension", "key", "label", "trips"],
        _category_rows,
    ),
    "corridors": Dataset(
        "corridors", "Green corridor outcomes",
        "Preemptions per day by outcome.",
        ["date", "activated", "yielded", "failed", "cancelled", "pending"],
        _corridor_rows,
    ),
    "response-distribution": Dataset(
        "response-distribution", "Response time distribution",
        "Histogram of response times against the 8-minute target.",
        ["bucket", "trips", "share", "within_target"],
        _response_rows,
    ),
    "hospitals": Dataset(
        "hospitals", "Hospital load",
        "Trips routed to each hospital, with crew override rate.",
        ["code", "name", "trips", "overrides", "override_share", "avg_transport_s"],
        _hospital_rows,
    ),
    "hotspots": Dataset(
        "hotspots", "Accident hotspots",
        "Clustered incident locations (feature 4.9).",
        ["kind", "label", "latitude", "longitude", "incidents", "score", "window_days"],
        _hotspot_rows,
    ),
}


def catalogue() -> list[dict]:
    return [
        {
            "key": dataset.key,
            "title": dataset.title,
            "description": dataset.description,
            "columns": dataset.headers,
            "url": f"/api/v1/analytics/export/{dataset.key}.csv",
        }
        for dataset in DATASETS.values()
    ]


def stream_csv(key: str, days: int) -> StreamingHttpResponse:
    dataset = DATASETS[key]
    writer = csv.writer(_Echo())

    def rows() -> Iterator[str]:
        # A header row naming the window and generation time, because a bare
        # CSV in a shared drive six months later is unattributable - and an
        # analytics export whose window nobody remembers gets compared against
        # one with a different window.
        # One cell per line: a two-element row renders as
        # "# window...,# generated..." and reads as two columns of data.
        yield writer.writerow([f"# SEVPS {dataset.title}"])
        yield writer.writerow([f"# window: last {days} days"])
        yield writer.writerow([f"# generated: {timezone.localtime().isoformat()}"])
        yield writer.writerow(dataset.headers)
        for row in dataset.rows(days):
            yield writer.writerow(row)

    stamp = timezone.localdate().isoformat()
    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="sevps-{key}-{stamp}.csv"'
    return response
