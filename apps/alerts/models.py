"""Layer 4 - advance warning to the drivers who are actually in the way.

The problem statement's observation is the design brief: drivers notice an
ambulance far too late.  So alerts are targeted *ahead* of the vehicle along
its planned route - not in a circle around it, which would mostly warn people
it has already passed - and they are delivered through whatever channel the
road user is already looking at.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.core.enums import AlertChannel, PriorityLevel
from apps.core.models import GeoPointModel, TimeStampedModel, UUIDModel


class DisplayBoard(TimeStampedModel, GeoPointModel):
    """A digital road display board / smart-city information display."""

    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=140)
    channel = models.CharField(
        max_length=16, choices=AlertChannel.choices, default=AlertChannel.VMS_BOARD
    )
    #: Direction of the traffic that reads this board, in compass degrees.
    facing_deg = models.FloatField(default=0.0)
    segment = models.ForeignKey(
        "network.RoadSegment", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="display_boards",
    )
    endpoint = models.URLField(blank=True, help_text="Sign controller API endpoint")
    is_active = models.BooleanField(default=True, db_index=True)
    current_message = models.CharField(max_length=300, blank=True)
    message_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"

    @property
    def is_displaying_alert(self) -> bool:
        return bool(
            self.current_message
            and self.message_expires_at
            and self.message_expires_at > timezone.now()
        )


class DriverDevice(TimeStampedModel, GeoPointModel):
    """A road user's app or navigation client subscribed to alerts.

    Only the coarse cell a device is in matters for delivery, so precise
    positions are kept only as long as they are useful for targeting; the
    ``geohash`` column is what the fan-out actually uses.
    """

    device_id = models.CharField(max_length=128, unique=True, db_index=True)
    channel = models.CharField(
        max_length=16, choices=AlertChannel.choices, default=AlertChannel.MOBILE_APP
    )
    push_token = models.CharField(max_length=255, blank=True)
    geohash = models.CharField(max_length=12, blank=True, db_index=True)
    heading_deg = models.FloatField(default=0.0)
    speed_kmh = models.FloatField(default=0.0)
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-last_seen_at"]

    def __str__(self) -> str:
        return f"Device {self.device_id[:12]}"

    def update_position(self, lat: float, lon: float, **kwargs) -> None:
        from apps.core.geo import geohash as encode

        self.latitude = lat
        self.longitude = lon
        self.geohash = encode(lat, lon, 6)
        self.heading_deg = kwargs.get("heading_deg", self.heading_deg)
        self.speed_kmh = kwargs.get("speed_kmh", self.speed_kmh)
        self.last_seen_at = timezone.now()
        self.save(
            update_fields=[
                "latitude", "longitude", "geohash", "heading_deg",
                "speed_kmh", "last_seen_at", "updated_at",
            ]
        )


class DriverAlert(TimeStampedModel, UUIDModel, GeoPointModel):
    """One advance-warning message issued for one emergency vehicle."""

    trip = models.ForeignKey(
        "dispatch.EmergencyTrip", on_delete=models.CASCADE, related_name="driver_alerts"
    )
    channel = models.CharField(
        max_length=16, choices=AlertChannel.choices, default=AlertChannel.MOBILE_APP
    )
    board = models.ForeignKey(
        DisplayBoard, on_delete=models.SET_NULL, null=True, blank=True, related_name="alerts"
    )
    message = models.CharField(max_length=300)
    instruction = models.CharField(max_length=120, default="Please move to the left lane")
    #: Seconds until the emergency vehicle reaches this location.
    eta_seconds = models.FloatField()
    radius_m = models.FloatField(default=800.0)
    #: Approach bearing of the emergency vehicle - lets a client show an arrow.
    approach_bearing_deg = models.FloatField(default=0.0)
    priority_level = models.PositiveSmallIntegerField(choices=PriorityLevel.choices)
    geohash = models.CharField(max_length=12, blank=True, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    delivered_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["trip", "-created_at"]),
            models.Index(fields=["geohash", "expires_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.message[:48]} ({self.eta_seconds:.0f}s)"

    @property
    def is_live(self) -> bool:
        return self.expires_at > timezone.now()

    def as_payload(self) -> dict:
        return {
            "id": self.id,
            "uuid": str(self.uuid),
            "trip_id": self.trip_id,
            "message": self.message,
            "instruction": self.instruction,
            "eta_seconds": round(self.eta_seconds),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "radius_m": self.radius_m,
            "approach_bearing_deg": round(self.approach_bearing_deg, 1),
            "priority_level": self.priority_level,
            "channel": self.channel,
            "expires_at": self.expires_at,
            "geohash": self.geohash,
        }
