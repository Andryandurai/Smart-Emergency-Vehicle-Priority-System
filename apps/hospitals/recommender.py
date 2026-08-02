"""Intelligent hospital recommendation (Layer 5 / feature 4.3).

Pipeline, in order - the order is the whole point:

1. **Rule** - the selected emergency category resolves to a mandatory
   facility set (Rule-Based Emergency Engine).
2. **Hard filter** - hospitals that lack a required facility, are inactive,
   are on diversion, or have no bed of the required kind are *excluded*, with
   the reason recorded.  A near hospital that cannot treat the patient is not
   a candidate at any distance.
3. **Route** - each surviving candidate is costed with the same
   time-dependent router used for the corridor, so "closest" means closest in
   *minutes under current traffic*, not in kilometres.
4. **Score** - a transparent weighted sum over capability, travel time, bed
   availability, workload and quality.
5. **Explain** - every candidate carries its per-factor scores and its
   exclusion reason, so the ranking can be defended afterwards.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from apps.brain.router import estimate_travel_time_s
from apps.core.enums import HospitalFacility, PriorityLevel
from apps.core.geo import Point, haversine_m
from apps.hospitals.rules import ResolvedRule, resolve_rule

log = logging.getLogger("sevps.hospitals")


#: Why a hospital was filtered out.  The kind matters because the fallback
#: path may relax some exclusions and must never relax others.
EXCLUSION_DIVERSION = "diversion"
EXCLUSION_CAPABILITY = "capability"
EXCLUSION_CAPACITY = "capacity"


@dataclass
class Candidate:
    hospital: object
    distance_m: float
    travel_time_s: float | None = None
    score: float = 0.0
    factors: dict = field(default_factory=dict)
    eligible: bool = True
    exclusion_reason: str = ""
    exclusion_kind: str = ""
    #: Reasons this hospital would normally have been excluded, retained when
    #: the engine had to relax a constraint.  Never silently dropped - the
    #: crew and the audit log must see what was given up.
    warnings: list = field(default_factory=list)
    missing_facilities: list = field(default_factory=list)
    within_golden_window: bool | None = None

    def as_dict(self) -> dict:
        h = self.hospital
        capacity = h.capacity
        return {
            "hospital_id": h.id,
            "code": h.code,
            "name": h.name,
            "latitude": h.latitude,
            "longitude": h.longitude,
            "phone": h.emergency_phone or h.phone,
            "distance_m": round(self.distance_m, 1),
            "distance_km": round(self.distance_m / 1000.0, 2),
            "travel_time_s": round(self.travel_time_s, 1) if self.travel_time_s else None,
            "travel_time_min": (
                round(self.travel_time_s / 60.0, 1) if self.travel_time_s else None
            ),
            "score": round(self.score, 4),
            "factors": {k: round(v, 4) for k, v in self.factors.items()},
            "eligible": self.eligible,
            "exclusion_reason": self.exclusion_reason,
            "exclusion_kind": self.exclusion_kind,
            "warnings": self.warnings,
            "missing_facilities": self.missing_facilities,
            "within_golden_window": self.within_golden_window,
            "emergency_beds_available": capacity.emergency_beds_available,
            "icu_beds_available": capacity.icu_beds_available,
            "workload_index": capacity.workload_index,
            "is_on_diversion": h.is_on_diversion,
            "capacity_is_stale": capacity.is_stale,
        }


@dataclass
class Recommendation:
    rule: ResolvedRule
    recommended: object | None
    candidates: list[Candidate]
    considered: int
    eligible_count: int
    relaxed: bool = False
    relaxation_note: str = ""

    def as_dict(self) -> dict:
        return {
            "rule": self.rule.as_dict(),
            "recommended": (
                next(
                    (c.as_dict() for c in self.candidates if c.hospital.id == self.recommended.id),
                    None,
                )
                if self.recommended
                else None
            ),
            "candidates": [c.as_dict() for c in self.candidates],
            "considered": self.considered,
            "eligible_count": self.eligible_count,
            "relaxed": self.relaxed,
            "relaxation_note": self.relaxation_note,
            "generated_at": timezone.now(),
        }


def _score_candidate(
    candidate: Candidate,
    rule: ResolvedRule,
    *,
    worst_travel_s: float,
    weights: dict,
) -> None:
    """Fill in per-factor scores (each 0..1) and the weighted total."""
    hospital = candidate.hospital
    capacity = hospital.capacity
    available = hospital.facility_codes

    # -- capability: required facilities are already guaranteed, so this
    #    factor measures the *depth* of capability beyond the minimum.
    if rule.preferred:
        preferred_cover = len(rule.preferred & available) / len(rule.preferred)
    else:
        preferred_cover = 1.0
    capability = 0.7 * preferred_cover + 0.3 * (1.0 if hospital.is_trauma_designated else 0.0)

    # -- travel time: linear against the worst candidate in this decision.
    travel = candidate.travel_time_s or worst_travel_s
    travel_score = 1.0 - (travel / worst_travel_s if worst_travel_s > 0 else 0.0)

    # -- beds: emergency capacity, plus ICU when the rule demands it.
    bed_score = min(1.0, capacity.emergency_beds_available / 8.0)
    if rule.requires_icu:
        bed_score = 0.5 * bed_score + 0.5 * min(1.0, capacity.icu_beds_available / 4.0)

    # -- workload and quality
    workload_score = 1.0 - capacity.workload_index
    quality_score = max(0.0, min(1.0, hospital.quality_index))

    # Stale capacity data should not be rewarded as though it were fresh.
    if capacity.is_stale:
        bed_score *= 0.6
        workload_score *= 0.6

    candidate.factors = {
        "capability": capability,
        "travel_time": travel_score,
        "bed_availability": bed_score,
        "workload": workload_score,
        "quality": quality_score,
    }
    candidate.score = sum(
        weights.get(name, 0.0) * value for name, value in candidate.factors.items()
    )

    # A time-critical presentation that cannot be delivered inside its golden
    # window is heavily penalised rather than excluded - a late arrival at a
    # capable hospital still beats an early arrival at an incapable one.
    if rule.golden_window_min and candidate.travel_time_s:
        within = candidate.travel_time_s <= rule.golden_window_min * 60
        candidate.within_golden_window = within
        if not within:
            penalty = 0.25 if rule.time_critical else 0.12
            candidate.score -= penalty
            candidate.factors["golden_window_penalty"] = -penalty


def recommend_hospital(
    origin: Point,
    category: str,
    *,
    priority_level: int | None = None,
    radius_km: float | None = None,
    max_candidates: int | None = None,
    exclude_hospital_ids: set[int] | None = None,
) -> Recommendation:
    """Recommend the most suitable hospital for a patient at ``origin``."""
    from apps.hospitals.models import Hospital

    cfg = settings.SEVPS
    rule = resolve_rule(category)
    priority_level = priority_level or rule.priority_level
    radius_m = (radius_km or cfg["HOSPITAL_SEARCH_RADIUS_KM"]) * 1000
    limit = max_candidates or cfg["HOSPITAL_MAX_CANDIDATES"]
    weights = cfg["HOSPITAL_WEIGHTS"]
    exclude = exclude_hospital_ids or set()

    nearby = [
        h
        for h in Hospital.objects.filter(is_active=True)
        .prefetch_related("capabilities")
        .select_related("capacity_row")
        .near(origin.lat, origin.lon, radius_m)
        if h.id not in exclude
    ]

    candidates = [Candidate(hospital=h, distance_m=h.distance_m) for h in nearby]
    _apply_hard_filters(candidates, rule)

    eligible = [c for c in candidates if c.eligible]
    relaxed, note = False, ""

    if not eligible:
        # Safety net: never leave a crew with nowhere to go.  Relaxed in
        # tiers, weakest constraint first, so the least is given up.  A
        # hospital that has formally declared diversion is never overridden -
        # that is a clinical decision the algorithm has no standing to reverse,
        # and if it leaves no candidate at all the case escalates to a human.
        relaxed = True
        note = (
            f"No hospital within {radius_m / 1000:.0f} km satisfies "
            f"{', '.join(sorted(rule.required)) or 'the rule'}. "
            "Relaxed to the nearest capable-enough emergency department - "
            "onward transfer likely."
        )
        eligible = _relax(candidates, {EXCLUSION_CAPABILITY}, note)

        if not eligible:
            note = (
                "No hospital within range has both the required facilities and "
                "a free bed. Relaxed capacity as well - receiving hospital will "
                "be over capacity on arrival."
            )
            eligible = _relax(candidates, {EXCLUSION_CAPABILITY, EXCLUSION_CAPACITY}, note)

        if not eligible:
            note = (
                "Every hospital in range is on diversion. SEVPS will not "
                "override a declared diversion - escalate to the control room."
            )
            log.error("Hospital recommendation failed for category=%s: %s", category, note)

    # Route only the shortlist - routing every hospital in a 25 km radius
    # would be wasteful, and straight-line order is a good pre-filter.
    eligible.sort(key=lambda c: c.distance_m)
    shortlist = eligible[: max(limit, 1)]
    for candidate in shortlist:
        candidate.travel_time_s = estimate_travel_time_s(
            origin,
            Point(candidate.hospital.latitude, candidate.hospital.longitude),
            priority_level,
        )

    worst = max((c.travel_time_s or 0.0) for c in shortlist) if shortlist else 1.0
    worst = max(worst, 1.0)
    for candidate in shortlist:
        _score_candidate(candidate, rule, worst_travel_s=worst, weights=weights)

    shortlist.sort(key=lambda c: -c.score)
    excluded = [c for c in candidates if not c.eligible]
    excluded.sort(key=lambda c: c.distance_m)

    return Recommendation(
        rule=rule,
        recommended=shortlist[0].hospital if shortlist else None,
        candidates=shortlist + excluded[:5],
        considered=len(candidates),
        eligible_count=len(eligible),
        relaxed=relaxed,
        relaxation_note=note,
    )


def _apply_hard_filters(candidates: list[Candidate], rule: ResolvedRule) -> None:
    """Exclude hospitals that cannot take this patient, recording why.

    Order matters: diversion first (a formal refusal outranks everything),
    then capability, then capacity.  The recorded ``exclusion_kind`` is what
    the fallback path uses to decide which constraints it may relax.
    """
    for candidate in candidates:
        hospital = candidate.hospital

        if hospital.is_on_diversion:
            candidate.eligible = False
            candidate.exclusion_kind = EXCLUSION_DIVERSION
            candidate.exclusion_reason = (
                f"on diversion: {hospital.diversion_reason or 'not accepting arrivals'}"
            )
            continue

        missing = hospital.missing_facilities(rule.required)
        if missing:
            candidate.eligible = False
            candidate.exclusion_kind = EXCLUSION_CAPABILITY
            candidate.missing_facilities = sorted(missing)
            candidate.exclusion_reason = f"missing required facilities: {', '.join(sorted(missing))}"
            continue

        can_accept, reason = hospital.capacity.can_accept(rule.requires_icu)
        if not can_accept:
            candidate.eligible = False
            candidate.exclusion_kind = EXCLUSION_CAPACITY
            candidate.exclusion_reason = reason


def _relax(candidates: list[Candidate], kinds: set[str], note: str) -> list[Candidate]:
    """Re-admit candidates excluded for the given reasons, keeping the reason.

    The original exclusion becomes a *warning* on the candidate rather than
    vanishing, so a relaxed recommendation is visibly relaxed everywhere it is
    displayed or logged.
    """
    for candidate in candidates:
        if candidate.eligible or candidate.exclusion_kind not in kinds:
            continue
        # Even when relaxing, a hospital with no emergency department is not
        # a receiving facility at all.
        if HospitalFacility.EMERGENCY_DEPT not in candidate.hospital.facility_codes:
            continue
        candidate.eligible = True
        candidate.warnings.append(candidate.exclusion_reason)
        candidate.exclusion_reason = ""
        candidate.exclusion_kind = ""
    log.warning("Hospital recommendation relaxed: %s", note)
    return [c for c in candidates if c.eligible]


def quick_nearest_capable(origin: Point, category: str) -> object | None:
    """Cheap lookup used by the simulator and by fallback paths."""
    recommendation = recommend_hospital(origin, category, max_candidates=3)
    return recommendation.recommended
