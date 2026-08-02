"""Persisted analytics artefacts.

Most figures in the analytics dashboards are computed on demand from the
operational tables - correct by construction and always current.  Only two
things are materialised here: rolled-up daily metrics (so a year of history
does not require scanning a year of telemetry) and identified hotspots (so
the clustering result is stable enough for authorities to act on).
"""
from __future__ import annotations

from django.db import models

from apps.core.models import GeoPointModel, TimeStampedModel


class DailyMetric(TimeStampedModel):
    """One day's rolled-up operational figures. Written by ``rollup_metrics``."""

    date = models.DateField(db_index=True)
    city = models.CharField(max_length=80, default="Chennai", db_index=True)

    trips_total = models.PositiveIntegerField(default=0)
    trips_completed = models.PositiveIntegerField(default=0)
    trips_cancelled = models.PositiveIntegerField(default=0)

    avg_response_time_s = models.FloatField(null=True, blank=True)
    median_response_time_s = models.FloatField(null=True, blank=True)
    p90_response_time_s = models.FloatField(null=True, blank=True)
    avg_transport_time_s = models.FloatField(null=True, blank=True)

    green_corridors_created = models.PositiveIntegerField(default=0)
    signals_preempted = models.PositiveIntegerField(default=0)
    total_hold_seconds = models.FloatField(default=0.0)
    avg_eta_error_s = models.FloatField(null=True, blank=True)

    reroutes = models.PositiveIntegerField(default=0)
    driver_alerts_issued = models.PositiveIntegerField(default=0)
    hospital_overrides = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["date", "city"], name="uniq_daily_metric")
        ]
        ordering = ["-date"]

    def __str__(self) -> str:
        return f"{self.city} {self.date}"


class Hotspot(TimeStampedModel, GeoPointModel):
    """A recurring problem location identified from history (feature 4.9)."""

    class Kind(models.TextChoices):
        ACCIDENT = "accident", "Accident-prone location"
        CONGESTION = "congestion", "Recurring congestion"
        DELAY = "delay", "High-delay intersection"

    kind = models.CharField(max_length=12, choices=Kind.choices, db_index=True)
    label = models.CharField(max_length=200, blank=True)
    intersection = models.ForeignKey(
        "network.Intersection", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="hotspots",
    )
    #: Count of contributing events in the analysis window.
    incident_count = models.PositiveIntegerField(default=0)
    #: 0..1 normalised severity so kinds can be compared on one map.
    score = models.FloatField(default=0.0)
    window_days = models.PositiveIntegerField(default=180)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-score"]
        indexes = [models.Index(fields=["kind", "-score"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} @ {self.label or f'{self.latitude:.4f},{self.longitude:.4f}'}"
