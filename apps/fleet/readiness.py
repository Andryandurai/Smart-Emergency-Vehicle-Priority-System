"""Turning an inspection into a fleet decision.

The checklist on its own is a form. What makes it matter is that its answers
*ground the vehicle* - a failed critical item sets readiness to NOT_READY,
raises a dated maintenance report against the vehicle, and tells fleet
management and dispatch, without the driver having to do any of that
separately or being able to skip it.

Everything here is derived. A driver records what they observed; nobody types
"ready" into a field. That is deliberate: the moment readiness becomes an
assertion rather than a consequence, it can be asserted over a brake failure.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.core.enums import MaintenanceState, VehicleReadiness
from apps.core.realtime import broadcast_ops

log = logging.getLogger("sevps.fleet.readiness")


def set_readiness(vehicle, readiness: str, *, broadcast: bool = True) -> None:
    """Write a vehicle's readiness and tell every open dashboard."""
    if vehicle.readiness == readiness:
        return
    vehicle.readiness = readiness
    vehicle.readiness_updated_at = timezone.now()
    vehicle.save(update_fields=["readiness", "readiness_updated_at", "updated_at"])
    if broadcast:
        broadcast_ops("vehicle_readiness", vehicle.as_fleet_row())


def apply_readiness(check, *, actor=None) -> dict:
    """Apply an inspection's outcome to its vehicle.

    Returns the parts of the answer the driver's screen needs that are not on
    the check itself: whether the vehicle may now be dispatched, and the
    maintenance report if one was raised.

    Idempotent by design - the driver's app saves partial progress repeatedly,
    and each save re-derives readiness from the full answer set rather than
    accumulating side effects.
    """
    from apps.core import notifications
    from apps.fleet.maintenance import MaintenanceReport

    shift = check.shift
    vehicle = shift.vehicle
    readiness = check.derived_readiness()

    report = None
    if readiness == VehicleReadiness.NOT_READY:
        report = _open_or_update_report(check, vehicle, shift, actor)

    elif vehicle.readiness == VehicleReadiness.NOT_READY:
        # The crew went back and passed the item that had grounded the
        # vehicle. Close the inspection-raised report rather than leaving a
        # stale fault on a vehicle that is demonstrably fine - a board full
        # of resolved-but-open faults is a board nobody reads.
        closed = MaintenanceReport.objects.filter(
            vehicle=vehicle, shift=shift, from_inspection=True,
            state__in=[MaintenanceState.OPEN, MaintenanceState.ACKNOWLEDGED],
        ).update(
            state=MaintenanceState.RESOLVED,
            resolved_at=timezone.now(),
            resolution_notes="Re-inspection passed; no critical faults remaining.",
        )
        if closed:
            log.info("%s: %d inspection fault(s) cleared by re-check", vehicle.callsign, closed)

    set_readiness(vehicle, readiness)

    if report is not None:
        notifications.vehicle_not_ready(vehicle, report)
        broadcast_ops("maintenance_report", report.as_payload())

    return {
        "vehicle_readiness": vehicle.readiness,
        "vehicle_readiness_display": vehicle.get_readiness_display(),
        "may_dispatch": vehicle.readiness
        not in {VehicleReadiness.NOT_READY, VehicleReadiness.MAINTENANCE},
        "maintenance_report": report.as_payload() if report else None,
    }


def _open_or_update_report(check, vehicle, shift, actor):
    """One live inspection report per shift, kept current.

    Updated rather than duplicated: a driver correcting an answer three times
    should leave one accurate fault record, not three contradictory ones for
    the workshop to reconcile.
    """
    from apps.fleet.maintenance import MaintenanceReport

    report = MaintenanceReport.objects.filter(
        vehicle=vehicle, shift=shift, from_inspection=True,
        state__in=[MaintenanceState.OPEN, MaintenanceState.ACKNOWLEDGED],
    ).first()

    remarks = _remarks_from(check)
    if report is None:
        return MaintenanceReport.objects.create(
            vehicle=vehicle,
            shift=shift,
            reported_by=actor,
            reasons=check.failed_reasons,
            failed_items=check.missing_codes,
            remarks=remarks,
            from_inspection=True,
        )

    report.reasons = check.failed_reasons
    report.failed_items = check.missing_codes
    report.remarks = remarks
    report.save(update_fields=["reasons", "failed_items", "remarks", "updated_at"])
    return report


def _remarks_from(check) -> str:
    """Collect the driver's per-item notes into the report's remarks.

    The workshop reads the report, not the checklist JSON, so a note left
    against "Brakes" has to travel with the fault or it may as well not have
    been written.
    """
    from apps.fleet.crew import EQUIPMENT_BY_CODE

    lines = []
    for code in check.missing_codes:
        label = EQUIPMENT_BY_CODE.get(code, {}).get("label", code)
        note = (check.items or {}).get(code, {}).get("note", "")
        lines.append(f"{label}: {note}" if note else label)
    if check.notes:
        lines.append(f"Driver notes: {check.notes}")
    return "\n".join(lines)
