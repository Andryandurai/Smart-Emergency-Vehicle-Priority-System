"""Symptom-driven triage - the "I am not sure what this is" path.

The rule engine matches a *category* to hospital facilities. That works when
the crew can name the presentation, and fails exactly when it matters most:
an unresponsive patient at a roadside with no history, where forcing a guess
between "cardiac" and "stroke" produces a confident wrong answer and routes
the patient to a hospital that cannot treat them.

So the paramedic can instead record what they can actually see. This module
turns those observations into the same three things a category supplies -
required facilities, preferred facilities and a priority level - which means
everything downstream (hospital scoring, siren policy, green corridor) works
unchanged.

Design notes worth keeping:

* Facilities are **unioned, not intersected**. A patient who is unconscious
  *and* bleeding needs somewhere that can handle both. Narrowing the hospital
  list is the safe direction to be wrong in only when the requirement is
  genuinely mandatory, which is why this list stays short and each entry is
  defensible on its own.
* Priority is the **most urgent** symptom's, never an average. Averaging
  chest pain with fever produces a Level 3 response to a heart attack.
* ``EMERGENCY_DEPT`` is implicit everywhere and deliberately not listed - it
  would exclude nothing, and a required-facility list that always matches
  teaches operators to ignore it.
"""
from __future__ import annotations

from apps.core.enums import HospitalFacility as F
from apps.core.enums import PatientSymptom as S
from apps.core.enums import PriorityLevel


class SymptomProfile:
    """Clinical consequences of one observed symptom."""

    __slots__ = ("required", "preferred", "priority", "note")

    def __init__(self, *, required=(), preferred=(), priority=PriorityLevel.MODERATE, note=""):
        self.required = tuple(required)
        self.preferred = tuple(preferred)
        self.priority = priority
        self.note = note


#: One entry per symptom the app offers. Reviewed as a clinical artefact -
#: changing a required facility changes where patients are taken.
SYMPTOM_PROFILES: dict[str, SymptomProfile] = {
    S.UNCONSCIOUS: SymptomProfile(
        required=(F.ICU,),
        preferred=(F.CT_SCAN, F.VENTILATOR, F.NEUROLOGY),
        priority=PriorityLevel.CRITICAL,
        note="Unprotected airway is the immediate risk; needs ICU-capable receiving.",
    ),
    S.CHEST_PAIN: SymptomProfile(
        required=(F.CARDIAC_ICU,),
        preferred=(F.CATH_LAB, F.ICU),
        priority=PriorityLevel.CRITICAL,
        note="Treat as cardiac until excluded - cath lab access decides outcome.",
    ),
    S.BREATHING_DIFFICULTY: SymptomProfile(
        required=(F.VENTILATOR,),
        preferred=(F.ICU,),
        priority=PriorityLevel.CRITICAL,
        note="Deterioration can be rapid; ventilator support must be available on arrival.",
    ),
    S.PARALYSIS: SymptomProfile(
        required=(F.STROKE_UNIT,),
        preferred=(F.CT_SCAN, F.NEUROLOGY, F.MRI),
        priority=PriorityLevel.CRITICAL,
        note="Possible stroke - thrombolysis window makes the receiving unit decisive.",
    ),
    S.SEIZURE: SymptomProfile(
        required=(F.NEUROLOGY,),
        preferred=(F.CT_SCAN, F.ICU),
        priority=PriorityLevel.HIGH,
        note="Needs neurology assessment; ICU if status epilepticus.",
    ),
    S.BURNS: SymptomProfile(
        required=(F.BURN_UNIT,),
        preferred=(F.ICU, F.OPERATION_THEATRE),
        priority=PriorityLevel.CRITICAL,
        note="Burn unit capability is not substitutable.",
    ),
    S.BLEEDING: SymptomProfile(
        required=(F.BLOOD_BANK,),
        preferred=(F.TRAUMA_CENTER, F.OPERATION_THEATRE),
        priority=PriorityLevel.HIGH,
        note="Transfusion capability decides survivable haemorrhage.",
    ),
    S.FRACTURE: SymptomProfile(
        required=(),
        preferred=(F.OPERATION_THEATRE, F.CT_SCAN, F.TRAUMA_CENTER),
        priority=PriorityLevel.MODERATE,
        note="Imaging and theatre access; rarely time-critical on its own.",
    ),
    S.VOMITING: SymptomProfile(
        required=(),
        preferred=(F.TOXICOLOGY, F.ICU),
        priority=PriorityLevel.MODERATE,
        note="Consider poisoning or head injury if with other symptoms.",
    ),
    S.FEVER: SymptomProfile(
        required=(),
        preferred=(F.ICU,),
        priority=PriorityLevel.NON_CRITICAL,
        note="Escalate only when combined with other findings.",
    ),
}

#: Symptom pairs that mean more together than apart. Kept explicit and short:
#: a general inference engine here would be unauditable, and clinical
#: governance has to be able to read every rule that moves a patient.
SYMPTOM_COMBINATIONS: list[tuple[frozenset[str], tuple[str, ...], str]] = [
    (
        frozenset({S.UNCONSCIOUS, S.BLEEDING}),
        (F.TRAUMA_CENTER, F.OPERATION_THEATRE),
        "Unconscious with bleeding - treat as major trauma.",
    ),
    (
        frozenset({S.PARALYSIS, S.UNCONSCIOUS}),
        (F.CT_SCAN,),
        "Paralysis with reduced consciousness - imaging needed on arrival.",
    ),
    (
        frozenset({S.CHEST_PAIN, S.BREATHING_DIFFICULTY}),
        (F.CATH_LAB,),
        "Chest pain with breathlessness - assume acute coronary syndrome.",
    ),
    (
        frozenset({S.VOMITING, S.UNCONSCIOUS}),
        (F.TOXICOLOGY,),
        "Vomiting with reduced consciousness - consider poisoning.",
    ),
]


class SymptomAssessment:
    """What a set of observed symptoms implies."""

    def __init__(self, symptoms):
        self.symptoms: list[str] = [s for s in symptoms if s in SYMPTOM_PROFILES]
        self.required: set[str] = set()
        self.preferred: set[str] = set()
        self.notes: list[str] = []
        # With no symptoms recorded the priority must not default to
        # "critical"; start at the least urgent and let evidence raise it.
        self.priority: int = PriorityLevel.NON_CRITICAL

        for code in self.symptoms:
            profile = SYMPTOM_PROFILES[code]
            self.required.update(profile.required)
            self.preferred.update(profile.preferred)
            # IntegerChoices: 1 is most urgent, so "most urgent wins" is min().
            self.priority = min(self.priority, profile.priority)
            if profile.note:
                self.notes.append(f"{SYMPTOM_LABELS[code]}: {profile.note}")

        selected = frozenset(self.symptoms)
        for combination, facilities, note in SYMPTOM_COMBINATIONS:
            if combination <= selected:
                self.required.update(facilities)
                self.notes.append(note)

        # A facility cannot be both mandatory and merely nice to have.
        self.preferred -= self.required

    @property
    def is_empty(self) -> bool:
        return not self.symptoms

    def as_dict(self) -> dict:
        return {
            "symptoms": self.symptoms,
            "labels": [SYMPTOM_LABELS[s] for s in self.symptoms],
            "required_facilities": sorted(self.required),
            "preferred_facilities": sorted(self.preferred),
            "priority_level": int(self.priority),
            "notes": self.notes,
        }


SYMPTOM_LABELS = {value: label for value, label in S.choices}


def catalogue() -> list[dict]:
    """The picker's contents, with the clinical consequence of each choice.

    Shipping the consequence to the client is deliberate: a crew choosing
    "Paralysis" should be able to see that it will send the patient to a
    stroke unit, before they choose it.
    """
    return [
        {
            "code": code,
            "label": SYMPTOM_LABELS[code],
            "priority_level": int(profile.priority),
            "required_facilities": list(profile.required),
            "note": profile.note,
        }
        for code, profile in SYMPTOM_PROFILES.items()
    ]


def assess(symptoms) -> SymptomAssessment:
    return SymptomAssessment(symptoms or [])
