"""The city road graph, its signals, its sensors and everything blocking it.

This app owns the *world model*: nodes (intersections), directed edges (road
segments), the signal controllers attached to those nodes, the camera feeds
that observe them, and the live/historical traffic state used by the AI engine
in :mod:`apps.brain`.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.core.enums import (
    FREE_FLOW_KMH,
    CongestionLevel,
    EventSource,
    RoadClass,
    RoadEventType,
    SignalPhase,
)
from apps.core.geo import Point, haversine_m, polyline_length_m
from apps.core.models import GeoPointModel, GeoQuerySet, TimeStampedModel, UUIDModel


class Intersection(TimeStampedModel, GeoPointModel):
    """A node in the routing graph."""

    osm_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    name = models.CharField(max_length=160, blank=True)
    city = models.CharField(max_length=80, default="Chennai", db_index=True)
    is_signalised = models.BooleanField(default=False, db_index=True)
    #: Average seconds lost at this node in free-flow conditions (signal wait,
    #: turn friction).  Added to edge traversal cost by the routing engine.
    base_delay_s = models.FloatField(default=0.0)

    class Meta:
        indexes = [models.Index(fields=["latitude", "longitude"])]
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name or f"Node {self.pk}"

    @property
    def label(self) -> str:
        return self.name or f"Node {self.pk}"


class RoadSegmentQuerySet(GeoQuerySet):
    def active(self):
        return self.filter(is_open=True)

    def stale(self, seconds: int = 300):
        """Segments whose live speed has not been refreshed recently."""
        cutoff = timezone.now() - timezone.timedelta(seconds=seconds)
        return self.filter(models.Q(speed_updated_at__lt=cutoff) | models.Q(speed_updated_at=None))


class RoadSegment(TimeStampedModel, GeoPointModel):
    """A directed edge between two intersections.

    ``latitude``/``longitude`` (inherited) hold the segment midpoint so the
    generic radius search in :class:`~apps.core.models.GeoQuerySet` works for
    edges as well as nodes.
    """

    from_node = models.ForeignKey(
        Intersection, on_delete=models.CASCADE, related_name="outgoing"
    )
    to_node = models.ForeignKey(Intersection, on_delete=models.CASCADE, related_name="incoming")
    name = models.CharField(max_length=160, blank=True)
    road_class = models.CharField(
        max_length=20, choices=RoadClass.choices, default=RoadClass.SECONDARY, db_index=True
    )
    length_m = models.FloatField()
    lanes = models.PositiveSmallIntegerField(default=2)
    free_flow_kmh = models.FloatField(default=40.0)
    #: Detailed shape as [[lat, lon], ...]; falls back to the node pair.
    geometry = models.JSONField(default=list, blank=True)
    is_open = models.BooleanField(default=True, db_index=True)
    #: Emergency vehicles may legally use the opposite carriageway here.
    allows_contraflow = models.BooleanField(default=False)

    # --- live state, refreshed by sensing / providers / CV -----------------
    current_speed_kmh = models.FloatField(null=True, blank=True)
    congestion_level = models.CharField(
        max_length=12, choices=CongestionLevel.choices, default=CongestionLevel.FREE, db_index=True
    )
    speed_updated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    #: Blended congestion penalty in [0, 1]; 0 = free flow, 1 = standstill.
    congestion_index = models.FloatField(default=0.0)

    objects = RoadSegmentQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["from_node", "to_node"]),
            models.Index(fields=["is_open", "congestion_level"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["from_node", "to_node"], name="uniq_directed_segment"
            ),
            models.CheckConstraint(check=models.Q(length_m__gt=0), name="segment_length_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.name or 'segment'} ({self.from_node_id}->{self.to_node_id})"

    # -- geometry -----------------------------------------------------------
    @property
    def shape(self) -> list[Point]:
        if self.geometry:
            return [Point(lat, lon) for lat, lon in self.geometry]
        return [self.from_node.point, self.to_node.point]

    def geojson_coordinates(self) -> list[list[float]]:
        return [[p.lon, p.lat] for p in self.shape]

    # -- speed / cost -------------------------------------------------------
    @property
    def design_speed_kmh(self) -> float:
        return self.free_flow_kmh or FREE_FLOW_KMH.get(self.road_class, 40.0)

    def effective_speed_kmh(self, emergency_multiplier: float = 1.0) -> float:
        """Speed a vehicle can realistically hold on this link right now.

        ``emergency_multiplier`` models the advantage a priority vehicle gains
        from right-of-way; it can never exceed the road's design speed by more
        than 25% - a cleared road does not make the road wider.
        """
        base = self.current_speed_kmh if self.current_speed_kmh else self.design_speed_kmh
        boosted = base * emergency_multiplier
        return max(5.0, min(boosted, self.design_speed_kmh * 1.25))

    def travel_time_s(self, emergency_multiplier: float = 1.0) -> float:
        return self.length_m / (self.effective_speed_kmh(emergency_multiplier) / 3.6)

    def apply_speed(self, speed_kmh: float, *, save: bool = True) -> None:
        """Record an observed speed and derive the congestion state from it."""
        design = self.design_speed_kmh
        self.current_speed_kmh = max(1.0, min(speed_kmh, design * 1.3))
        ratio = self.current_speed_kmh / design if design else 1.0
        self.congestion_level = CongestionLevel.from_ratio(ratio)
        self.congestion_index = round(max(0.0, min(1.0, 1.0 - ratio)), 4)
        self.speed_updated_at = timezone.now()
        if save:
            self.save(
                update_fields=[
                    "current_speed_kmh",
                    "congestion_level",
                    "congestion_index",
                    "speed_updated_at",
                    "updated_at",
                ]
            )

    def recompute_geometry(self, *, save: bool = True) -> None:
        """Refresh midpoint and length from the stored shape."""
        shape = self.shape
        self.length_m = max(1.0, polyline_length_m(shape)) if len(shape) > 1 else self.length_m
        mid = shape[len(shape) // 2]
        self.latitude, self.longitude = mid.lat, mid.lon
        if save:
            self.save(update_fields=["length_m", "latitude", "longitude", "updated_at"])

    def save(self, *args, **kwargs):
        if self.latitude is None or self.longitude is None:
            shape = self.shape
            mid = shape[len(shape) // 2]
            self.latitude, self.longitude = mid.lat, mid.lon
        return super().save(*args, **kwargs)


class TrafficSignal(TimeStampedModel, UUIDModel):
    """A signal controller attached to an intersection (Layer 3).

    SEVPS talks to real controllers over whatever southbound protocol the city
    exposes (NTCIP, vendor REST, or a serial bridge).  The controller adapter
    is pluggable - see :mod:`apps.dispatch.controllers`.
    """

    intersection = models.OneToOneField(
        Intersection, on_delete=models.CASCADE, related_name="signal"
    )
    controller_id = models.CharField(max_length=64, unique=True)
    #: Adapter key, resolved by apps.dispatch.controllers.get_controller()
    controller_type = models.CharField(max_length=32, default="simulated")
    endpoint = models.URLField(blank=True)
    cycle_seconds = models.PositiveIntegerField(default=120)
    #: Normal fixed-time plan: {"approach_id": {"green": 40, "amber": 3}}
    phase_plan = models.JSONField(default=dict, blank=True)
    supports_preemption = models.BooleanField(default=True)
    #: Minimum seconds of normal operation between two forced greens - protects
    #: cross traffic when several emergencies run back to back.
    min_recovery_s = models.PositiveIntegerField(default=45)

    current_phase = models.CharField(
        max_length=16, choices=SignalPhase.choices, default=SignalPhase.RED
    )
    is_preempted = models.BooleanField(default=False, db_index=True)
    preempted_until = models.DateTimeField(null=True, blank=True)
    last_preempted_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    is_online = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["controller_id"]

    def __str__(self) -> str:
        return f"Signal {self.controller_id} @ {self.intersection.label}"

    @property
    def latitude(self) -> float:
        return self.intersection.latitude

    @property
    def longitude(self) -> float:
        return self.intersection.longitude

    def can_preempt_now(self) -> tuple[bool, str]:
        """Guard rail: is it safe and permitted to force a green right now?"""
        if not self.supports_preemption:
            return False, "controller does not support preemption"
        if not self.is_online:
            return False, "controller offline"
        if self.last_preempted_at:
            elapsed = (timezone.now() - self.last_preempted_at).total_seconds()
            if not self.is_preempted and elapsed < self.min_recovery_s:
                return False, f"recovery window active ({self.min_recovery_s - elapsed:.0f}s left)"
        return True, "ok"


class CameraFeed(TimeStampedModel, GeoPointModel):
    """An existing traffic camera reused for computer-vision analysis (4.5)."""

    name = models.CharField(max_length=120)
    intersection = models.ForeignKey(
        Intersection, on_delete=models.SET_NULL, null=True, blank=True, related_name="cameras"
    )
    #: Segment whose approach this camera observes, if known.
    segment = models.ForeignKey(
        RoadSegment, on_delete=models.SET_NULL, null=True, blank=True, related_name="cameras"
    )
    stream_url = models.CharField(max_length=500, blank=True)
    heading_deg = models.FloatField(default=0.0)
    is_active = models.BooleanField(default=True, db_index=True)
    last_analysed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class TrafficObservation(models.Model):
    """One measurement of a segment's state (from CV, provider or probe).

    This is the fact table behind historical pattern learning and the
    congestion forecaster.
    """

    segment = models.ForeignKey(
        RoadSegment, on_delete=models.CASCADE, related_name="observations"
    )
    observed_at = models.DateTimeField(default=timezone.now, db_index=True)
    speed_kmh = models.FloatField()
    vehicle_count = models.PositiveIntegerField(default=0)
    #: Vehicles per lane-kilometre.
    density = models.FloatField(default=0.0)
    occupancy = models.FloatField(default=0.0, help_text="Fraction of frame covered by vehicles")
    congestion_level = models.CharField(
        max_length=12, choices=CongestionLevel.choices, default=CongestionLevel.FREE
    )
    source = models.CharField(
        max_length=16, choices=EventSource.choices, default=EventSource.SIMULATION
    )
    camera = models.ForeignKey(
        CameraFeed, on_delete=models.SET_NULL, null=True, blank=True, related_name="observations"
    )

    class Meta:
        indexes = [
            models.Index(fields=["segment", "-observed_at"]),
            models.Index(fields=["-observed_at"]),
        ]
        ordering = ["-observed_at"]

    def __str__(self) -> str:
        return f"{self.segment_id} @ {self.observed_at:%H:%M} = {self.speed_kmh:.0f} km/h"


class TrafficProfile(models.Model):
    """Learned historical pattern: expected speed factor per weekday-hour cell.

    ``speed_factor`` is the ratio of observed speed to free-flow speed, so it
    transfers across roads of different design speeds.  Recomputed by
    ``python manage.py learn_traffic_profiles``.
    """

    segment = models.ForeignKey(RoadSegment, on_delete=models.CASCADE, related_name="profiles")
    weekday = models.PositiveSmallIntegerField(help_text="0=Monday .. 6=Sunday")
    hour = models.PositiveSmallIntegerField()
    speed_factor = models.FloatField(default=1.0)
    sample_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["segment", "weekday", "hour"], name="uniq_profile_cell"
            )
        ]
        indexes = [models.Index(fields=["segment", "weekday", "hour"])]

    def __str__(self) -> str:
        return f"{self.segment_id} d{self.weekday}h{self.hour}: x{self.speed_factor:.2f}"


class RoadEventQuerySet(GeoQuerySet):
    def active(self):
        now = timezone.now()
        return self.filter(
            is_active=True,
            starts_at__lte=now,
        ).filter(models.Q(ends_at__isnull=True) | models.Q(ends_at__gte=now))


class RoadEvent(TimeStampedModel, UUIDModel, GeoPointModel):
    """Anything degrading or blocking the network (4.2, 4.5, 4.10)."""

    event_type = models.CharField(max_length=20, choices=RoadEventType.choices, db_index=True)
    segment = models.ForeignKey(
        RoadSegment, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    description = models.CharField(max_length=300, blank=True)
    #: 0.0 = negligible, 1.0 = road fully impassable.
    severity = models.FloatField(default=0.5)
    confidence = models.FloatField(default=1.0)
    source = models.CharField(
        max_length=16, choices=EventSource.choices, default=EventSource.OPERATOR
    )
    starts_at = models.DateTimeField(default=timezone.now, db_index=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    #: Radius of influence for events not bound to a specific segment.
    radius_m = models.FloatField(default=120.0)

    objects = RoadEventQuerySet.as_manager()

    class Meta:
        ordering = ["-starts_at"]
        indexes = [models.Index(fields=["is_active", "event_type"])]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} ({self.severity:.0%})"

    @property
    def blocks_road(self) -> bool:
        return self.severity >= 0.95 or self.event_type in {
            RoadEventType.CLOSURE,
            RoadEventType.BLOCKAGE,
        }

    def affects(self, segment: RoadSegment) -> bool:
        """Does this event degrade the given segment?"""
        if self.segment_id:
            return self.segment_id == segment.id
        return haversine_m(self.latitude, self.longitude, segment.latitude, segment.longitude) <= (
            self.radius_m + segment.length_m / 2
        )


class AccidentRecord(TimeStampedModel, GeoPointModel):
    """Historical accident archive powering hotspot identification (4.9)."""

    occurred_at = models.DateTimeField(db_index=True)
    severity = models.PositiveSmallIntegerField(
        default=2, help_text="1=minor, 2=serious, 3=fatal"
    )
    intersection = models.ForeignKey(
        Intersection, on_delete=models.SET_NULL, null=True, blank=True, related_name="accidents"
    )
    casualties = models.PositiveSmallIntegerField(default=0)
    description = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["latitude", "longitude"])]

    def __str__(self) -> str:
        return f"Accident {self.occurred_at:%Y-%m-%d} sev={self.severity}"
