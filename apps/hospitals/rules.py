"""The Rule-Based Emergency Engine.

The paramedic selects one category in the app; this module turns that single
tap into a hard clinical requirement set, a priority level, a golden-hour
window and crew guidance.  The rules live in the database
(:class:`~apps.hospitals.models.EmergencyRule`) so clinical governance can
change them without a deployment; the table below is the seed, and matches
the mapping given in the problem statement.
"""
from __future__ import annotations

from dataclasses import dataclass

from apps.core.enums import EmergencyCategory as EC
from apps.core.enums import HospitalFacility as HF
from apps.core.enums import PriorityLevel as PL

#: Seed rule base.  Loaded by ``manage.py seed_rules`` (idempotent).
DEFAULT_RULES: list[dict] = [
    {
        "category": EC.CARDIAC,
        "display_name": "Heart Attack / Cardiac Arrest",
        "required_facilities": [HF.CARDIAC_ICU, HF.CATH_LAB],
        "preferred_facilities": [HF.ICU, HF.OPERATION_THEATRE, HF.VENTILATOR, HF.BLOOD_BANK],
        "default_priority_level": PL.CRITICAL,
        "requires_icu": True,
        "golden_window_min": 90,
        "time_critical": True,
        "guidance": (
            "Continuous 12-lead ECG monitoring. Transmit ECG ahead of arrival. "
            "Aspirin unless contraindicated. Cath lab to be activated pre-arrival."
        ),
    },
    {
        "category": EC.STROKE,
        "display_name": "Stroke",
        "required_facilities": [HF.NEUROLOGY, HF.CT_SCAN],
        "preferred_facilities": [HF.STROKE_UNIT, HF.ICU, HF.MRI],
        "default_priority_level": PL.CRITICAL,
        "requires_icu": True,
        # Thrombolysis window: door-to-needle must fit inside 4.5 h from onset,
        # so transport is planned against a 60 min operational target.
        "golden_window_min": 60,
        "time_critical": True,
        "guidance": (
            "Record time last seen well. FAST assessment. Nil by mouth. "
            "CT scanner to be cleared for immediate imaging on arrival."
        ),
    },
    {
        "category": EC.BURN,
        "display_name": "Burn",
        "required_facilities": [HF.BURN_UNIT],
        "preferred_facilities": [HF.ICU, HF.OPERATION_THEATRE, HF.VENTILATOR],
        "default_priority_level": PL.CRITICAL,
        "requires_icu": True,
        "golden_window_min": 120,
        "time_critical": False,
        "guidance": (
            "Estimate TBSA burned. Cool the burn, warm the patient. "
            "Early airway assessment if inhalation injury suspected."
        ),
    },
    {
        "category": EC.TRAUMA,
        "display_name": "Trauma / Major Accident",
        "required_facilities": [HF.TRAUMA_CENTER],
        "preferred_facilities": [
            HF.OPERATION_THEATRE, HF.BLOOD_BANK, HF.ICU, HF.CT_SCAN, HF.VENTILATOR,
        ],
        "default_priority_level": PL.CRITICAL,
        "requires_icu": True,
        "golden_window_min": 60,
        "time_critical": True,
        "guidance": (
            "Catastrophic haemorrhage control first. Spinal precautions. "
            "Activate trauma team and cross-match blood pre-arrival."
        ),
    },
    {
        "category": EC.POISONING,
        "display_name": "Poisoning",
        "required_facilities": [HF.TOXICOLOGY, HF.ICU],
        "preferred_facilities": [HF.VENTILATOR, HF.DIALYSIS],
        "default_priority_level": PL.HIGH,
        "requires_icu": True,
        "golden_window_min": 90,
        "time_critical": False,
        "guidance": (
            "Bring the container or a photograph of the substance. "
            "Do not induce vomiting. Note time and estimated quantity ingested."
        ),
    },
    {
        "category": EC.RESPIRATORY,
        "display_name": "Respiratory Distress",
        "required_facilities": [HF.EMERGENCY_DEPT, HF.VENTILATOR],
        "preferred_facilities": [HF.ICU],
        "default_priority_level": PL.HIGH,
        "requires_icu": True,
        "golden_window_min": 45,
        "time_critical": True,
        "guidance": "Titrate oxygen to saturation. Prepare for assisted ventilation.",
    },
    {
        "category": EC.OBSTETRIC,
        "display_name": "Obstetric Emergency",
        "required_facilities": [HF.OBSTETRICS, HF.OPERATION_THEATRE],
        "preferred_facilities": [HF.NICU, HF.BLOOD_BANK, HF.ICU],
        "default_priority_level": PL.HIGH,
        "requires_icu": False,
        "golden_window_min": 30,
        "time_critical": True,
        "guidance": "Left lateral position. Alert labour ward and neonatal team.",
    },
    {
        "category": EC.PEDIATRIC,
        "display_name": "Paediatric Emergency",
        "required_facilities": [HF.PICU, HF.EMERGENCY_DEPT],
        "preferred_facilities": [HF.NICU, HF.VENTILATOR, HF.OPERATION_THEATRE],
        "default_priority_level": PL.HIGH,
        "requires_icu": True,
        "golden_window_min": 45,
        "time_critical": True,
        "guidance": "Use weight-based dosing. Keep the child warm. Alert paediatric team.",
    },
    {
        "category": EC.FIRE_RESCUE,
        "display_name": "Fire / Rescue",
        "required_facilities": [HF.EMERGENCY_DEPT],
        "preferred_facilities": [HF.BURN_UNIT, HF.TRAUMA_CENTER, HF.ICU],
        "default_priority_level": PL.CRITICAL,
        "requires_icu": False,
        "golden_window_min": None,
        "time_critical": True,
        "guidance": "Scene safety first. Report casualty count early for triage.",
    },
    {
        "category": EC.TRANSFER,
        "display_name": "Non-critical Transfer",
        "required_facilities": [],
        "preferred_facilities": [HF.EMERGENCY_DEPT],
        "default_priority_level": PL.NON_CRITICAL,
        "requires_icu": False,
        "golden_window_min": None,
        "time_critical": False,
        "guidance": "Routine transport. Observe normal traffic regulations.",
    },
    {
        "category": EC.UNKNOWN,
        "display_name": "Undetermined",
        "required_facilities": [HF.EMERGENCY_DEPT],
        "preferred_facilities": [HF.ICU, HF.CT_SCAN, HF.TRAUMA_CENTER],
        "default_priority_level": PL.MODERATE,
        "requires_icu": False,
        "golden_window_min": None,
        "time_critical": False,
        "guidance": (
            "Category not yet determined - route to a full-service emergency "
            "department and update the category once assessment is complete."
        ),
    },
]


@dataclass
class ResolvedRule:
    """A rule flattened for use by the recommender and Layer 6."""

    category: str
    display_name: str
    required: set[str]
    preferred: set[str]
    priority_level: int
    requires_icu: bool
    golden_window_min: int | None
    time_critical: bool
    guidance: str

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "display_name": self.display_name,
            "required_facilities": sorted(self.required),
            "preferred_facilities": sorted(self.preferred),
            "priority_level": self.priority_level,
            "requires_icu": self.requires_icu,
            "golden_window_min": self.golden_window_min,
            "time_critical": self.time_critical,
            "guidance": self.guidance,
        }


def _from_seed(category: str) -> dict:
    for row in DEFAULT_RULES:
        if row["category"] == category:
            return row
    return next(r for r in DEFAULT_RULES if r["category"] == EC.UNKNOWN)


def resolve_rule(category: str) -> ResolvedRule:
    """Look up the active rule for a category.

    Falls back to the built-in seed when the database has not been seeded, so
    the engine is never left without a clinical rule.
    """
    from apps.hospitals.models import EmergencyRule

    rule = EmergencyRule.objects.filter(category=category, is_active=True).first()
    if rule is None:
        seed = _from_seed(category)
        return ResolvedRule(
            category=seed["category"],
            display_name=seed["display_name"],
            required=set(seed["required_facilities"]),
            preferred=set(seed["preferred_facilities"]),
            priority_level=seed["default_priority_level"],
            requires_icu=seed["requires_icu"],
            golden_window_min=seed["golden_window_min"],
            time_critical=seed["time_critical"],
            guidance=seed["guidance"],
        )
    return ResolvedRule(
        category=rule.category,
        display_name=rule.display_name,
        required=rule.required_set,
        preferred=rule.preferred_set,
        priority_level=rule.default_priority_level,
        requires_icu=rule.requires_icu,
        golden_window_min=rule.golden_window_min,
        time_critical=rule.time_critical,
        guidance=rule.guidance,
    )


def seed_rules() -> tuple[int, int]:
    """Load/refresh the seed rule base. Returns ``(created, updated)``."""
    from apps.hospitals.models import EmergencyRule

    created = updated = 0
    for row in DEFAULT_RULES:
        _, was_created = EmergencyRule.objects.update_or_create(
            category=row["category"],
            defaults={
                "display_name": row["display_name"],
                "required_facilities": list(row["required_facilities"]),
                "preferred_facilities": list(row["preferred_facilities"]),
                "default_priority_level": row["default_priority_level"],
                "requires_icu": row["requires_icu"],
                "golden_window_min": row["golden_window_min"],
                "time_critical": row["time_critical"],
                "guidance": row["guidance"],
                "is_active": True,
            },
        )
        created += int(was_created)
        updated += int(not was_created)
    return created, updated
