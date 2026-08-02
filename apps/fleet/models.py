"""Layer 1 - Emergency Vehicle Tracking Module.

Every ambulance, fire engine and police vehicle is bound to the platform via
the SEVPS mobile app or an onboard unit.  This app owns the live fleet state
(position, speed, heading, status, priority) and the telemetry history that
the AI engine uses to project where a vehicle will be next.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone

from apps.core.enums import (
    LightPattern,
    PriorityLevel,
    SirenMode,
    VehicleStatus,
    VehicleType,
)
from apps.core.geo import bearing_deg, destination_point, haversine_m
from apps.core.models import GeoPointModel, GeoQuerySet, TimeStampedModel, UUIDModel


class Station(TimeStampedModel, GeoPointModel):
    """A base station / fire house / hospital ambulance bay."""

    name = models.CharField(max_length=140)
    code = models.CharField(max_length=24, unique=True)
    city = models.CharField(max_length=80, default="Chennai")
    address = models.CharField(max_length=300, blank=True)
    contact_number = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class VehicleQuerySet(GeoQuerySet):
    """Fleet filters on top of the shared radius search.

    Extending :class:`~apps.core.models.GeoQuerySet` rather than plain
    ``QuerySet`` keeps ``.near()`` available - assigning a custom manager to a
    model would otherwise shadow the one inherited from ``GeoPointModel``, and
    "find the closest available ambulance" is the single most important query
    this model answers.
    """

    def online(self):
        return self.exclude(status=VehicleStatus.OFFLINE)

    def deployable(self):
        return self.filter(status=VehicleStatus.AVAILABLE)

    def on_mission(self):
        return self.filter(
            status__in=[
                VehicleStatus.DISPATCHED,
                VehicleStatus.ON_SCENE,
                VehicleStatus.TRANSPORTING,
            ]
        )

    def stale(self, seconds: int = 60):
        cutoff = timezone.now() - timezone.timedelta(seconds=seconds)
        return self.filter(models.Q(last_seen_at__lt=cutoff) | models.Q(last_seen_at=None))


class EmergencyVehicle(TimeStampedModel, UUIDModel, GeoPointModel):
    """A tracked priority vehicle."""

    callsign = models.CharField(max_length=32, unique=True, db_index=True)
    registration = models.CharField(max_length=24, blank=True)
    vehicle_type = models.CharField(
        max_length=20, choices=VehicleType.choices, default=VehicleType.AMBULANCE, db_index=True
    )
    operator = models.CharField(max_length=140, blank=True, help_text="Operating agency")
    home_station = models.ForeignKey(
        Station, on_delete=models.SET_NULL, null=True, blank=True, related_name="vehicles"
    )
    status = models.CharField(
        max_length=20, choices=VehicleStatus.choices, default=VehicleStatus.OFFLINE, db_index=True
    )

    # --- live tracking state ----------------------------------------------
    latitude = models.FloatField(default=0.0, db_index=True)
    longitude = models.FloatField(default=0.0, db_index=True)
    heading_deg = models.FloatField(default=0.0)
    speed_kmh = models.FloatField(default=0.0)
    accuracy_m = models.FloatField(default=10.0)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)

    # --- Layer 6 state (mirrored here so the onboard unit has one source) --
    priority_level = models.PositiveSmallIntegerField(
        choices=PriorityLevel.choices, default=PriorityLevel.NON_CRITICAL
    )
    siren_mode = models.CharField(
        max_length=16, choices=SirenMode.choices, default=SirenMode.OFF
    )
    light_pattern = models.CharField(
        max_length=16, choices=LightPattern.choices, default=LightPattern.OFF
    )

    # --- capability / crew --------------------------------------------------
    is_als = models.BooleanField(default=False, verbose_name="Advanced Life Support")
    crew_size = models.PositiveSmallIntegerField(default=2)
    equipment = models.JSONField(default=list, blank=True)
    device_token = models.CharField(max_length=255, blank=True, help_text="Push notification token")

    objects = VehicleQuerySet.as_manager()

    class Meta:
        ordering = ["callsign"]
        indexes = [
            models.Index(fields=["status", "vehicle_type"]),
            models.Index(fields=["latitude", "longitude"]),
        ]

    def __str__(self) -> str:
        return f"{self.callsign} ({self.get_vehicle_type_display()})"

    # -- tracking -----------------------------------------------------------
    @property
    def is_stale(self) -> bool:
        """No GPS fix in the last minute - treat the position as unreliable."""
        if not self.last_seen_at:
            return True
        return (timezone.now() - self.last_seen_at).total_seconds() > 60

    @property
    def active_trip(self):
        return self.trips.exclude(
            stage__in=["arrived", "handover", "cancelled"]
        ).order_by("-created_at").first()

    def project_position(self, seconds_ahead: float):
        """Dead-reckon where this vehicle will be in ``seconds_ahead``.

        Used as a fallback when no route is available; when the vehicle *is*
        on a planned route, :mod:`apps.brain.eta` projects along the polyline
        instead, which is far more accurate.
        """
        distance = (self.speed_kmh / 3.6) * max(0.0, seconds_ahead)
        return destination_point(self.latitude, self.longitude, self.heading_deg, distance)

    def record_position(
        self,
        latitude: float,
        longitude: float,
        *,
        speed_kmh: float | None = None,
        heading_deg: float | None = None,
        accuracy_m: float | None = None,
        recorded_at=None,
        persist_history: bool = True,
    ) -> "VehicleTelemetry":
        """Apply a GPS fix, deriving speed/heading when the device omits them."""
        now = recorded_at or timezone.now()
        previous = (self.latitude, self.longitude, self.last_seen_at)

        if heading_deg is None and previous[2] is not None:
            moved = haversine_m(previous[0], previous[1], latitude, longitude)
            heading_deg = (
                bearing_deg(previous[0], previous[1], latitude, longitude)
                if moved > 5
                else self.heading_deg
            )
        if speed_kmh is None and previous[2] is not None:
            dt = (now - previous[2]).total_seconds()
            moved = haversine_m(previous[0], previous[1], latitude, longitude)
            speed_kmh = (moved / dt) * 3.6 if dt > 0.5 else self.speed_kmh

        self.latitude = latitude
        self.longitude = longitude
        self.heading_deg = heading_deg if heading_deg is not None else self.heading_deg
        self.speed_kmh = max(0.0, speed_kmh if speed_kmh is not None else self.speed_kmh)
        self.accuracy_m = accuracy_m if accuracy_m is not None else self.accuracy_m
        self.last_seen_at = now
        if self.status == VehicleStatus.OFFLINE:
            self.status = VehicleStatus.AVAILABLE
        self.save(
            update_fields=[
                "latitude", "longitude", "heading_deg", "speed_kmh",
                "accuracy_m", "last_seen_at", "status", "updated_at",
            ]
        )

        telemetry = VehicleTelemetry(
            vehicle=self,
            latitude=latitude,
            longitude=longitude,
            speed_kmh=self.speed_kmh,
            heading_deg=self.heading_deg,
            accuracy_m=self.accuracy_m,
            recorded_at=now,
            trip=self.active_trip,
        )
        if persist_history:
            telemetry.save()
        return telemetry

    def as_tracking_payload(self) -> dict:
        """Compact form pushed over WebSockets to dashboards and the map."""
        return {
            "id": self.id,
            "uuid": str(self.uuid),
            "callsign": self.callsign,
            "vehicle_type": self.vehicle_type,
            "status": self.status,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "heading_deg": round(self.heading_deg, 1),
            "speed_kmh": round(self.speed_kmh, 1),
            "priority_level": self.priority_level,
            "siren_mode": self.siren_mode,
            "light_pattern": self.light_pattern,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "is_stale": self.is_stale,
        }


class VehicleTelemetry(models.Model):
    """Append-only GPS breadcrumb trail.

    Feeds route-adherence checks, response-time analytics and the historical
    speed profiles that the congestion model learns from.
    """

    vehicle = models.ForeignKey(
        EmergencyVehicle, on_delete=models.CASCADE, related_name="telemetry"
    )
    trip = models.ForeignKey(
        "dispatch.EmergencyTrip",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="telemetry",
    )
    latitude = models.FloatField()
    longitude = models.FloatField()
    speed_kmh = models.FloatField(default=0.0)
    heading_deg = models.FloatField(default=0.0)
    accuracy_m = models.FloatField(default=10.0)
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [
            models.Index(fields=["vehicle", "-recorded_at"]),
            models.Index(fields=["trip", "recorded_at"]),
        ]
        verbose_name_plural = "vehicle telemetry"

    def __str__(self) -> str:
        return f"{self.vehicle_id} @ {self.recorded_at:%H:%M:%S}"
