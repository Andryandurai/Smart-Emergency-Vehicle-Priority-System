"""What admitting a patient costs the ward.

The hospital portal's "Patient Received" says the ambulance is here. Admitting
is the separate, deliberate act of taking the patient in - and that is the
moment a bed stops being available to the recommender that is, right now,
choosing where to send the next one.

Until this existed, capacity only ever moved when somebody typed a new number
into the Updates tab. So a hospital could take four cardiac patients in an hour
and still advertise four free cardiac ICU beds, and SEVPS would keep routing to
it. Deriving the cost from the patient's own record closes that loop.

Two design points:

**Derived from the clinical record, not asked for.** The category and the
observed symptoms already exist on the trip, and the rule base already says
which facilities that category *requires* - so the resources a patient consumes
follow from what the crew recorded rather than from a form the receiving nurse
fills in while wheeling a trolley.

**A plan, then an apply.** :func:`plan_for_trip` computes and explains; it
touches nothing. The portal shows that plan on the Admit button so the ward can
see what is about to be deducted, and only then is it applied. A deduction
nobody could preview would be a deduction nobody trusts.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.core.enums import EmergencyCategory, HospitalFacility, PatientSymptom

log = logging.getLogger("sevps.hospitals.admission")

#: capacity field -> human label, for the preview and the audit line.
RESOURCE_LABELS = {
    "emergency_beds_available": "Emergency bed",
    "general_beds_available": "General bed",
    "icu_beds_available": "ICU bed",
    "pediatric_beds_available": "Pediatric bed",
    "burn_unit_beds_available": "Burn unit bed",
    "cardiac_icu_available": "Cardiac ICU bed",
    "ventilators_available": "Ventilator",
    "operation_theatres_free": "Operation theatre",
}

#: Category -> the ward that category lands in, beyond the emergency bed every
#: arrival occupies. Mirrors the rule base's required facilities rather than
#: inventing a second clinical mapping.
_CATEGORY_WARD = {
    EmergencyCategory.CARDIAC: "cardiac_icu_available",
    EmergencyCategory.BURN: "burn_unit_beds_available",
    EmergencyCategory.PEDIATRIC: "pediatric_beds_available",
}

#: Symptoms that mean a ventilator is committed on arrival.
_VENTILATOR_SYMPTOMS = {PatientSymptom.UNCONSCIOUS, PatientSymptom.BREATHING_DIFFICULTY}

#: Symptoms that mean a theatre is being held.
_THEATRE_SYMPTOMS = {PatientSymptom.BLEEDING, PatientSymptom.FRACTURE}


def plan_for_trip(trip) -> dict:
    """Work out what this patient will occupy, and why.

    Returns ``{"resources": [{field, label, units, reason}], "notes": [...]}``.
    Nothing is written.
    """
    # `resolve_rule_for` rather than `resolve_rule`: symptoms add to the
    # category's requirements, and a trauma patient who is also burned needs
    # the burn unit counted. Using the narrower lookup here would let the
    # ward's figures disagree with the rule the patient was routed on.
    from apps.hospitals.rules import resolve_rule_for

    category = trip.emergency_category
    symptoms = set(trip.symptoms or [])
    rule = resolve_rule_for(category, trip.symptoms or [])
    required = set(rule.required)

    plan: dict[str, dict] = {}

    def claim(field: str, reason: str) -> None:
        # First reason wins: a cardiac patient who is also unconscious needs
        # one ventilator, not two, and the clinical reason is the one that
        # explains the ward they are going to.
        plan.setdefault(field, {"field": field, "label": RESOURCE_LABELS[field], "units": 1, "reason": reason})

    # Every arrival takes an emergency bed. That is what "brought in by
    # ambulance" means, whatever is wrong with them.
    claim("emergency_beds_available", "Arrived by ambulance")

    ward = _CATEGORY_WARD.get(category)
    if ward:
        claim(ward, f"{trip.get_emergency_category_display()} admission")

    if rule.requires_icu or HospitalFacility.ICU in required:
        claim("icu_beds_available", "Category requires intensive care")

    if HospitalFacility.VENTILATOR in required or symptoms & _VENTILATOR_SYMPTOMS:
        reason = (
            "Airway compromised on arrival"
            if symptoms & _VENTILATOR_SYMPTOMS
            else "Category requires ventilation"
        )
        claim("ventilators_available", reason)

    if HospitalFacility.OPERATION_THEATRE in required or symptoms & _THEATRE_SYMPTOMS:
        reason = (
            "Surgical presentation - theatre held"
            if symptoms & _THEATRE_SYMPTOMS
            else "Category requires a theatre"
        )
        claim("operation_theatres_free", reason)

    # A patient who is not going to a specialist ward is going to a general
    # one. Without this, a stroke or a poisoning would occupy nothing but the
    # emergency bed they are about to be moved out of.
    specialist = {"icu_beds_available", "cardiac_icu_available", "burn_unit_beds_available", "pediatric_beds_available"}
    if not (specialist & plan.keys()):
        claim("general_beds_available", "Admitted to a general ward")

    notes = []
    if trip.patient_deteriorating:
        notes.append("Crew reported the patient deteriorating in transit.")
    if trip.patient_age is not None and trip.patient_age < 13 and "pediatric_beds_available" not in plan:
        notes.append("Patient is a child - a pediatric bed may be needed instead.")

    return {"resources": list(plan.values()), "notes": notes}


class InsufficientCapacity(Exception):
    """The ward cannot cover what this admission needs."""

    def __init__(self, shortfalls: list[dict]):
        self.shortfalls = shortfalls
        super().__init__("insufficient capacity")


@transaction.atomic
def admit(trip, hospital, *, actor: str = "") -> dict:
    """Take the patient in, and decrement what they occupy.

    Locked with ``select_for_update``: two nurses admitting two patients at the
    same second must not both read "1 ICU bed free" and both decrement it to
    zero. The whole point of this function is that the number is trustworthy.

    Refuses rather than going negative. A ward at zero ICU beds that admits an
    ICU patient anyway has not gained a bed - it has lost the only signal that
    would have told the recommender to stop sending them.
    """
    from apps.hospitals.models import HospitalCapacity

    plan = plan_for_trip(trip)
    capacity = (
        HospitalCapacity.objects.select_for_update().filter(hospital=hospital).first()
        or HospitalCapacity.objects.create(hospital=hospital)
    )

    shortfalls = [
        {**item, "available": getattr(capacity, item["field"])}
        for item in plan["resources"]
        if getattr(capacity, item["field"]) < item["units"]
    ]
    if shortfalls:
        raise InsufficientCapacity(shortfalls)

    applied = []
    for item in plan["resources"]:
        before = getattr(capacity, item["field"])
        setattr(capacity, item["field"], before - item["units"])
        applied.append({**item, "before": before, "after": before - item["units"]})

    capacity.reported_at = timezone.now()
    capacity.save(
        update_fields=[*{item["field"] for item in plan["resources"]}, "reported_at", "updated_at"]
    )

    log.info(
        "admitted %s to %s: %s (by %s)",
        trip.reference,
        hospital.code,
        ", ".join(f"{item['label']} -{item['units']}" for item in applied),
        actor or "hospital",
    )
    return {"applied": applied, "notes": plan["notes"]}
