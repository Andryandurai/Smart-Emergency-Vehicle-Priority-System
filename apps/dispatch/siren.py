"""Layer 6 - AI-Based Ambulance Light & Siren Priority Control.

Severity, not habit, decides how loud an ambulance is.  This module owns the
mapping from clinical state to the four priority levels, and from a level to
the concrete light pattern, siren mode and traffic-signal entitlement.

Why it matters beyond noise: a siren that runs continuously on every journey
stops meaning anything.  Reserving the continuous tone for Level 1 keeps it
informative, which is precisely what makes drivers yield when it matters.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from apps.core.enums import (
    LightPattern,
    PriorityLevel,
    SirenMode,
    TripStage,
    VehicleStatus,
)
from apps.hospitals.rules import resolve_rule_for

log = logging.getLogger("sevps.dispatch.siren")


@dataclass(frozen=True)
class PriorityProfile:
    level: int
    label: str
    siren_mode: str
    light_pattern: str
    grants_green_corridor: bool
    signal_priority: str
    description: str

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "label": self.label,
            "siren_mode": self.siren_mode,
            "light_pattern": self.light_pattern,
            "grants_green_corridor": self.grants_green_corridor,
            "signal_priority": self.signal_priority,
            "description": self.description,
        }


#: The four-level ladder exactly as specified in the problem statement.
PRIORITY_PROFILES: dict[int, PriorityProfile] = {
    PriorityLevel.CRITICAL: PriorityProfile(
        level=PriorityLevel.CRITICAL,
        label="Level 1 - Critical Emergency",
        siren_mode=SirenMode.CONTINUOUS,
        light_pattern=LightPattern.MAX_INTENSITY,
        grants_green_corridor=True,
        signal_priority="highest",
        description=(
            "Continuous high-priority siren, maximum-intensity flashing lights, "
            "automatic green corridor and highest traffic signal priority."
        ),
    ),
    PriorityLevel.HIGH: PriorityProfile(
        level=PriorityLevel.HIGH,
        label="Level 2 - High Emergency",
        siren_mode=SirenMode.INTERMITTENT,
        light_pattern=LightPattern.STANDARD,
        grants_green_corridor=True,
        signal_priority="congested_intersections",
        description=(
            "Standard emergency lights with intermittent siren and priority "
            "signal control at congested intersections."
        ),
    ),
    PriorityLevel.MODERATE: PriorityProfile(
        level=PriorityLevel.MODERATE,
        label="Level 3 - Moderate Emergency",
        siren_mode=SirenMode.BURST,
        light_pattern=LightPattern.FLASHING,
        grants_green_corridor=False,
        signal_priority="junctions_only",
        description=(
            "Flashing lights with short siren bursts only when required, "
            "primarily at junctions."
        ),
    ),
    PriorityLevel.NON_CRITICAL: PriorityProfile(
        level=PriorityLevel.NON_CRITICAL,
        label="Level 4 - Non-Critical Transport",
        siren_mode=SirenMode.OFF,
        light_pattern=LightPattern.OFF,
        grants_green_corridor=False,
        signal_priority="none",
        description="Lights and siren off; the ambulance follows normal traffic regulations.",
    ),
}


def profile_for(level: int) -> PriorityProfile:
    return PRIORITY_PROFILES.get(level, PRIORITY_PROFILES[PriorityLevel.NON_CRITICAL])


def derive_priority(trip) -> tuple[int, str]:
    """Determine the correct priority level for a trip right now.

    Returns ``(level, trigger)``.  The trigger string is stored on the
    directive so the reason for every change is visible after the fact.
    """
    # Resolved with the crew's observations, not the category alone. An
    # unconscious, bleeding patient recorded under "Undetermined" - which is
    # the correct entry for a crew who cannot yet name the presentation - was
    # otherwise dispatched at the undetermined rule's Level 3, so no green
    # corridor and no siren for a patient the crew had already described as
    # critical. The observations are the evidence; ignoring them here made
    # Layer 6 the one part of the platform that did not listen to them.
    rule = resolve_rule_for(trip.emergency_category, trip.symptoms)
    base = rule.priority_level

    # Before a patient is on board there is no clinical severity to act on -
    # only the reported nature of the call.  Response to the scene is urgent
    # but capped at Level 2 unless the call itself is a Level 1 category.
    if trip.stage in {TripStage.CREATED, TripStage.TO_SCENE}:
        level = min(base, PriorityLevel.HIGH) if base > PriorityLevel.CRITICAL else base
        return level, "responding to scene"

    if trip.stage == TripStage.ON_SCENE:
        # Stationary at the scene: lights on for visibility and scene safety,
        # siren off - it serves no purpose and distresses the patient.
        return PriorityLevel.MODERATE, "on scene - scene safety lighting"

    if trip.stage == TripStage.TO_HOSPITAL:
        if trip.patient_deteriorating and base > PriorityLevel.CRITICAL:
            return PriorityLevel.CRITICAL, "patient condition deteriorating"
        return base, f"transporting {rule.display_name.lower()} patient"

    # Arrived, handed over, cancelled, or returning to base.
    return PriorityLevel.NON_CRITICAL, "no patient on board"


def apply_priority(
    trip,
    level: int | None = None,
    *,
    trigger: str = "",
    rationale: str = "",
    issued_by: str = "sevps-layer6",
    force: bool = False,
):
    """Set a trip's priority and push the directive to the vehicle.

    Returns the created :class:`~apps.dispatch.models.PriorityDirective`, or
    ``None`` when nothing changed (the common case on a routine GPS tick).
    """
    from apps.core.realtime import broadcast, broadcast_ops, vehicle_group
    from apps.dispatch.models import PriorityDirective

    if level is None:
        level, derived_trigger = derive_priority(trip)
        trigger = trigger or derived_trigger

    profile = profile_for(level)
    unchanged = (
        trip.priority_level == level
        and trip.siren_mode == profile.siren_mode
        and trip.light_pattern == profile.light_pattern
    )
    if unchanged and not force:
        return None

    previous = trip.priority_level
    trip.priority_level = level
    trip.siren_mode = profile.siren_mode
    trip.light_pattern = profile.light_pattern
    trip.condition_updated_at = timezone.now()
    trip.save(
        update_fields=[
            "priority_level", "siren_mode", "light_pattern",
            "condition_updated_at", "updated_at",
        ]
    )

    # Mirror onto the vehicle so the onboard unit has a single source of truth.
    vehicle = trip.vehicle
    vehicle.priority_level = level
    vehicle.siren_mode = profile.siren_mode
    vehicle.light_pattern = profile.light_pattern
    vehicle.save(
        update_fields=["priority_level", "siren_mode", "light_pattern", "updated_at"]
    )

    directive = PriorityDirective.objects.create(
        trip=trip,
        priority_level=level,
        previous_level=previous,
        siren_mode=profile.siren_mode,
        light_pattern=profile.light_pattern,
        grants_green_corridor=profile.grants_green_corridor,
        trigger=trigger or "priority reassessment",
        rationale=rationale or profile.description,
        issued_by=issued_by,
    )

    payload = {
        "trip_id": trip.id,
        "reference": trip.reference,
        "vehicle": vehicle.callsign,
        "directive_id": directive.id,
        "previous_level": previous,
        **profile.as_dict(),
        "trigger": directive.trigger,
        "rationale": directive.rationale,
        "is_upgrade": directive.is_upgrade,
        "is_downgrade": directive.is_downgrade,
    }
    broadcast(vehicle_group(vehicle.callsign), "priority_directive", payload)
    broadcast_ops("priority_directive", payload)

    log.info(
        "Trip %s priority %s -> %s (%s)", trip.reference, previous, level, directive.trigger
    )

    # Dropping to Level 4 means the vehicle no longer needs the corridor.
    if level == PriorityLevel.NON_CRITICAL and previous != PriorityLevel.NON_CRITICAL:
        from apps.dispatch.corridor import release_corridor

        release_corridor(trip, reason="priority downgraded to Level 4")

    return directive


def stand_down(vehicle, *, status: str = VehicleStatus.AVAILABLE) -> None:
    """Return a vehicle to normal running - lights and siren off."""
    vehicle.priority_level = PriorityLevel.NON_CRITICAL
    vehicle.siren_mode = SirenMode.OFF
    vehicle.light_pattern = LightPattern.OFF
    vehicle.status = status
    vehicle.save(
        update_fields=["priority_level", "siren_mode", "light_pattern", "status", "updated_at"]
    )
