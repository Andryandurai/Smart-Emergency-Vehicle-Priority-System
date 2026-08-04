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
    ShiftStatus,
    SirenMode,
    VehicleOwnership,
    VehicleReadiness,
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
        """Free to be sent to a call.

        Readiness is a separate gate applied at dispatch rather than folded in
        here: this queryset also answers "which ambulances exist and are
        idle", and a grounded vehicle still needs to appear in that answer.
        """
        return self.filter(status=VehicleStatus.AVAILABLE)

    def dispatchable(self):
        """Free to be sent to a call *and* fit to go.

        Excludes vehicles grounded by a failed inspection or in the workshop.
        ``TEMPORARILY_READY`` is included by design - that is the emergency
        skip, whose entire purpose is to let a vehicle roll with the
        inspection still owed.
        """
        return self.deployable().exclude(
            readiness__in=[VehicleReadiness.NOT_READY, VehicleReadiness.MAINTENANCE]
        )

    def selectable_for_takeover(self):
        """Ambulances a driver may take over at the start of a shift.

        Available, not already crewed, and not grounded. The open-shift
        exclusion is what stops two drivers claiming the same vehicle from
        the picker before the unique constraint rejects the second one.

        DRAFT counts as taken. It was omitted here while the uniqueness rule
        and ``claim`` both used ``CrewShift.objects.open()``, which includes
        it - so an ambulance another driver was standing at, part-way through
        its inspection, was still offered in the picker and then refused with
        a 409 on the tap. The three statuses must match ``open()`` exactly or
        the picker and the constraint disagree again.

        Demonstration units are excluded outright rather than by status. They
        spend most of their time on a synthetic response and so are already
        filtered by the AVAILABLE test - but for the seconds between one demo
        trip finishing and the next starting they would surface in the picker,
        and a driver is not helped by an ambulance that drives itself away.
        """
        return (
            self.filter(status=VehicleStatus.AVAILABLE, is_demo=False)
            .exclude(readiness__in=[VehicleReadiness.NOT_READY, VehicleReadiness.MAINTENANCE])
            .exclude(
                shifts__status__in=[
                    ShiftStatus.DRAFT,
                    ShiftStatus.PENDING,
                    ShiftStatus.ACTIVE,
                ]
            )
        )

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
    ownership = models.CharField(
        max_length=20,
        choices=VehicleOwnership.choices,
        default=VehicleOwnership.GOVERNMENT,
        db_index=True,
        help_text="Operating sector - decides billing, escalation contact and dispatch agreement",
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

    # --- readiness (Driver module) -----------------------------------------
    #: Fitness for dispatch, distinct from ``status`` which is what the
    #: vehicle is *doing*. A vehicle can be AVAILABLE and NOT_READY at once.
    #: Derived from the shift's inspection - see EquipmentCheck.
    readiness = models.CharField(
        max_length=20,
        choices=VehicleReadiness.choices,
        default=VehicleReadiness.UNCHECKED,
        db_index=True,
    )
    readiness_updated_at = models.DateTimeField(null=True, blank=True)

    # --- capability / crew --------------------------------------------------
    is_als = models.BooleanField(default=False, verbose_name="Advanced Life Support")
    #: A permanently-running demonstration unit.
    #:
    #: Kept on a rolling synthetic response by :mod:`apps.dispatch.journey` so
    #: there is always traffic on the map to look at. Excluded from the
    #: takeover picker: a driver who claimed one would be handed an ambulance
    #: that is already carrying a patient somewhere.
    is_demo = models.BooleanField(
        default=False,
        verbose_name="Demonstration unit",
        help_text="Always on a synthetic response. Cannot be taken over by a driver.",
    )
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
            "registration": self.registration,
            "vehicle_type": self.vehicle_type,
            "vehicle_type_display": self.get_vehicle_type_display(),
            "ownership": self.ownership,
            "ownership_display": self.get_ownership_display(),
            "operator": self.operator,
            "is_als": self.is_als,
            "status": self.status,
            "status_display": self.get_status_display(),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "heading_deg": round(self.heading_deg, 1),
            "speed_kmh": round(self.speed_kmh, 1),
            "priority_level": self.priority_level,
            "siren_mode": self.siren_mode,
            "light_pattern": self.light_pattern,
            "readiness": self.readiness,
            "readiness_display": self.get_readiness_display(),
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "is_stale": self.is_stale,
        }

    def as_fleet_row(self) -> dict:
        """One row of the admin fleet board.

        Joins the four things an operations manager has to correlate by hand
        otherwise: where the vehicle is, who is on it, what it is doing, and
        whether it is fit to do it. Callers are expected to have
        ``select_related``/``prefetch_related`` the shift and trip - this is
        rendered for the whole fleet on a timer, and a lazy load per row is
        an N+1 on the busiest screen in the system.
        """
        # DRAFT counts as crewed. A driver standing at the vehicle part-way
        # through its inspection has it; reporting "No crew signed on" made
        # the board disagree with both the uniqueness constraint and the
        # takeover picker, which offered an ambulance somebody was already
        # holding. The three statuses must match ``CrewShift.objects.open()``.
        shift = next(
            (s for s in self.shifts.all() if s.status in {"draft", "pending", "active"}),
            None,
        )
        trip = self.active_trip
        return {
            **self.as_tracking_payload(),
            "is_demo": self.is_demo,
            # --- crew -------------------------------------------------------
            "shift_status": shift.status if shift else "no_shift",
            "shift_status_display": shift.get_status_display() if shift else "No crew signed on",
            "driver_name": (
                shift.driver.get_full_name() or shift.driver.get_username()
            ) if shift else None,
            # Null on a DRAFT shift by design: the driver has claimed the
            # vehicle and has not named anybody yet.
            "paramedic_name": (
                shift.paramedic.get_full_name() or shift.paramedic.get_username()
            ) if shift and shift.paramedic_id else None,
            "on_duty_since": shift.accepted_at if shift else None,
            # --- inspection -------------------------------------------------
            "inspection_status": self._inspection_status(shift),
            # --- current job ------------------------------------------------
            "current_trip_reference": trip.reference if trip else None,
            "current_emergency": trip.get_emergency_category_display() if trip else None,
            "current_priority_level": trip.priority_level if trip else None,
            "current_destination": (
                trip.destination_hospital.name
                if trip and trip.destination_hospital else None
            ),
            "current_eta": trip.eta if trip else None,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def _inspection_status(shift) -> str:
        """Plain words for the fleet board, not an enum the reader must decode."""
        check = getattr(shift, "equipment_check", None) if shift else None
        # A check row with nothing answered is "not started", not "in progress
        # (0/21)" - the row is created with the shift, and reporting it as
        # started would hide exactly the vehicles that owe an inspection.
        if check is None or check.answered_count == 0 and not check.skipped:
            return "not started"
        if check.missing_critical:
            return "failed"
        if check.is_complete:
            return "complete"
        if check.skipped:
            return "skipped - pending"
        return f"in progress ({check.answered_count}/{len(EQUIPMENT_CATALOGUE)})"


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


# ---------------------------------------------------------------------------
# Crew takeover and the start-of-shift vehicle check.
#   Kept in their own module for length, re-exported here so
#   ``from apps.fleet.models import CrewShift`` works like every other model.
# ---------------------------------------------------------------------------
from apps.fleet.crew import (  # noqa: E402,F401  (circular-safe: crew imports no models)
    CRITICAL_CODES,
    EQUIPMENT_BY_CODE,
    EQUIPMENT_CATALOGUE,
    CrewShift,
    EquipmentCheck,
    failure_reasons_for,
)
from apps.fleet.maintenance import (  # noqa: E402,F401
    BreakdownEvent,
    MaintenanceReport,
    TransferOffer,
)
