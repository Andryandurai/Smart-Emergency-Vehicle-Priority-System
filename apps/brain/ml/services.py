"""Bind the estimators to real SEVPS state.

Estimators take feature dicts; the rest of the platform has trips, segments
and signals. This module is the only place that knows how to turn one into the
other, so a feature definition changes in exactly one file and the models,
training commands and API endpoints all follow.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.brain.ml import estimators as est
from apps.brain.ml.base import Explanation, FeatureContribution, Prediction

log = logging.getLogger("sevps.ml.services")


# ---------------------------------------------------------------------------
# Congestion
# ---------------------------------------------------------------------------
def congestion_features(edge, horizon_s: float, historical: float, live: float | None) -> dict:
    when = timezone.localtime() + timezone.timedelta(seconds=horizon_s)
    return {
        **est.time_features(when),
        "road_class_index": est.road_class_index(edge.road_class),
        "design_kmh": edge.design_kmh,
        "lanes": edge.lanes,
        "live_factor": live,
        "horizon_min": horizon_s / 60.0,
        "historical_factor": historical,
    }


def predict_congestion(segment_id: int, minutes_ahead: float = 10.0) -> Prediction:
    """Speed factor for one segment N minutes out, with confidence and SHAP."""
    from apps.brain import graph as graph_mod
    from apps.brain.congestion import build_forecaster, default_speed_factor

    state = graph_mod.get_state()
    edge = state.edges.get(segment_id)
    if edge is None:
        raise ValueError(f"unknown segment {segment_id}")

    horizon_s = minutes_ahead * 60
    forecaster = build_forecaster()
    when = timezone.localtime() + timezone.timedelta(seconds=horizon_s)
    historical, _ = forecaster._historical_factor(segment_id, when)
    live = forecaster._live_factor(edge)

    features = congestion_features(edge, horizon_s, historical, live)
    prediction = est.CONGESTION.predict(features)
    prediction.context.update(
        {
            "segment_id": segment_id,
            "segment_name": edge.name,
            "design_kmh": edge.design_kmh,
            "predicted_speed_kmh": round(edge.design_kmh * float(prediction.value), 1),
            "forecast_for": when.isoformat(),
        }
    )
    return prediction


# ---------------------------------------------------------------------------
# ETA
# ---------------------------------------------------------------------------
def predict_eta(trip) -> Prediction:
    """The router's ETA plus a learned correction.

    The value returned is the *corrected remaining seconds*; the residual and
    the router's own figure are both in the context so a caller can see how
    much the model moved it and decide whether to trust that.
    """
    from apps.brain.eta import compute_progress

    plan = trip.active_route
    if plan is None:
        raise ValueError("trip has no active route")

    progress = compute_progress(plan, trip.vehicle.point)
    steps = plan.steps or []
    signalised = sum(1 for step in steps if step.get("is_signalised_exit"))
    speeds = [float(s.get("predicted_speed_kmh", 0) or 0) for s in steps]
    designs = [s for s in speeds if s > 0]
    mean_congestion = (
        1.0 - (sum(designs) / len(designs)) / 50.0 if designs else 0.5
    )

    features = {
        **est.time_features(),
        "planned_duration_s": plan.total_duration_s,
        "planned_distance_m": plan.total_distance_m,
        "signalised_count": signalised,
        "priority_level": trip.priority_level,
        "mean_congestion": max(0.0, min(1.0, mean_congestion)),
        "vehicle_speed_kmh": trip.vehicle.speed_kmh,
        "completion": progress.completion,
    }

    residual = est.ETA_RESIDUAL.predict(features)
    router_remaining = progress.remaining_s
    corrected = max(0.0, router_remaining + float(residual.value))

    residual.context.update(
        {
            "trip_id": trip.id,
            "reference": trip.reference,
            "router_remaining_s": round(router_remaining, 1),
            "residual_s": round(float(residual.value), 1),
            "eta": (timezone.now() + timezone.timedelta(seconds=corrected)).isoformat(),
        }
    )
    # The headline value is the answer a caller wants: corrected seconds.
    residual.value = round(corrected, 1)
    residual.unit = "seconds"
    return residual


# ---------------------------------------------------------------------------
# Emergency priority (advisory)
# ---------------------------------------------------------------------------
CATEGORY_INDEX = {
    "cardiac": 0, "stroke": 1, "burn": 2, "trauma": 3, "poisoning": 4,
    "respiratory": 5, "obstetric": 6, "pediatric": 7, "fire_rescue": 8,
    "transfer": 9, "unknown": 10,
}


def predict_priority(
    category: str,
    *,
    patient_age: int | None = None,
    deteriorating: bool = False,
) -> Prediction:
    """Advisory priority level, checked against the authoritative rule.

    The rule engine's level is always what the platform acts on. When the
    model expects something different *and* is confident, the disagreement is
    recorded on the prediction so a dispatcher sees it.
    """
    from apps.hospitals.rules import resolve_rule

    rule = resolve_rule(category)
    features = {
        **est.time_features(),
        "category_index": CATEGORY_INDEX.get(category, 10),
        "patient_age": patient_age if patient_age is not None else 45,
        "deteriorating": int(bool(deteriorating)),
        "requires_icu": int(rule.requires_icu),
        "golden_window_min": rule.golden_window_min or 0,
        "time_critical": int(rule.time_critical),
        "is_transfer": int(category == "transfer"),
        # Used only by the baseline, which returns the rule's own answer.
        "rule_level": rule.priority_level,
        "category": category,
    }

    prediction = est.EMERGENCY_PRIORITY.predict(features)
    rule_level = int(rule.priority_level)
    model_level = int(prediction.value)

    prediction.context.update(
        {
            "rule_level": rule_level,
            "model_level": model_level,
            "authoritative_level": rule_level,
            "category": category,
            "note": (
                "The Rule-Based Emergency Engine is authoritative for priority. "
                "This model advises only."
            ),
        }
    )
    if prediction.source == "model" and model_level != rule_level and prediction.is_actionable:
        prediction.disagreement = (
            f"Model expects Level {model_level} but the clinical rule for "
            f"{rule.display_name} gives Level {rule_level}. The rule stands; "
            f"review whether the recorded category matches the presentation."
        )
    # Never let the advisory value be mistaken for the decision.
    prediction.value = rule_level
    return prediction


# ---------------------------------------------------------------------------
# Green corridor
# ---------------------------------------------------------------------------
def predict_corridor_success(signal, *, seconds_to_arrival: float, priority_level: int,
                             congestion_index: float = 0.0, lanes: int = 2) -> Prediction:
    """Will this junction actually grant the green?"""
    from apps.dispatch.models import SignalPreemption

    since = timezone.now() - timezone.timedelta(minutes=15)
    recent = SignalPreemption.objects.filter(signal=signal, created_at__gte=since).count()

    features = {
        **est.time_features(),
        "seconds_to_arrival": seconds_to_arrival,
        "congestion_index": congestion_index,
        "lanes": lanes,
        "priority_level": priority_level,
        "supports_preemption": int(signal.supports_preemption),
        "recent_preemptions": recent,
        "min_recovery_s": signal.min_recovery_s,
        "is_online": int(signal.is_online),
    }

    prediction = est.CORRIDOR_SUCCESS.predict(features)
    prediction.context.update(
        {"controller_id": signal.controller_id, "recent_preemptions": recent}
    )

    # The deterministic guard rails outrank the model in both directions: a
    # controller that is offline cannot grant a green no matter what a model
    # learned from history.
    if not signal.is_online or not signal.supports_preemption:
        prediction.value = 0.0
        prediction.confidence = 1.0
        prediction.source = "rule"
        prediction.explanation = Explanation(
            method="rule",
            summary=(
                "Controller offline"
                if not signal.is_online
                else "Controller does not support preemption"
            ),
        )
    return prediction


def predict_clearance(*, congestion_index: float, lanes: int, road_class: str,
                      design_kmh: float, priority_level: int) -> Prediction:
    features = {
        **est.time_features(),
        "congestion_index": congestion_index,
        "lanes": lanes,
        "road_class_index": est.road_class_index(road_class),
        "design_kmh": design_kmh,
        "priority_level": priority_level,
    }
    return est.CLEARANCE_TIME.predict(features)


# ---------------------------------------------------------------------------
# Hospital recommendation, in the same envelope
# ---------------------------------------------------------------------------
def explain_hospital_recommendation(recommendation) -> Prediction:
    """Wrap the rule-based recommender in the standard prediction envelope.

    No SHAP here, and that is the point: the recommender is a deterministic
    weighted sum over five named factors, and it already exposes every one of
    them per candidate. Running an attribution method over arithmetic whose
    terms are already published would add ceremony, not insight - so the
    explanation reports the real weighted contributions instead.

    Confidence is the margin over the runner-up. A recommendation that barely
    beat second place is genuinely less certain than one that dominated, and
    a crew deciding whether to override deserves to know which they have.
    """
    from django.conf import settings

    top = recommendation.recommended
    if top is None:
        return Prediction(
            value=None,
            confidence=0.0,
            explanation=Explanation(
                method="rule",
                summary=recommendation.relaxation_note or "No hospital in range can accept.",
            ),
            model="hospital_recommendation",
            source="rule",
            context={"eligible_count": recommendation.eligible_count},
        )

    eligible = [c for c in recommendation.candidates if c.eligible]
    best = next((c for c in eligible if c.hospital.id == top.id), None)
    runner_up = next((c for c in eligible if c.hospital.id != top.id), None)

    margin = (best.score - runner_up.score) if (best and runner_up) else 0.5
    # A 0.15 margin is a clear win; below that the choice was close.
    confidence = max(0.25, min(0.99, 0.5 + margin * 3.0))

    weights = settings.SEVPS["HOSPITAL_WEIGHTS"]
    contributions = [
        FeatureContribution(
            name=factor,
            value=round(score, 3),
            contribution=weights.get(factor, 0.0) * score,
            detail=f"{factor.replace('_', ' ')} scored {score:.0%} at weight {weights.get(factor, 0):.0%}",
        )
        for factor, score in (best.factors if best else {}).items()
    ]

    return Prediction(
        value={"hospital_id": top.id, "code": top.code, "name": top.name},
        confidence=confidence,
        explanation=Explanation(
            method="rule_weighted",
            summary=(
                f"{top.name} scored {best.score:.0%}"
                + (f", {margin:.0%} ahead of {runner_up.hospital.name}" if runner_up else "")
                + f". Required facilities: {', '.join(sorted(recommendation.rule.required)) or 'emergency department'}."
            ),
            contributions=contributions,
        ),
        model="hospital_recommendation",
        source="rule",
        disagreement=recommendation.relaxation_note if recommendation.relaxed else "",
        context={
            "considered": recommendation.considered,
            "eligible": recommendation.eligible_count,
            "relaxed": recommendation.relaxed,
            "excluded": [
                {"name": c.hospital.name, "reason": c.exclusion_reason}
                for c in recommendation.candidates
                if not c.eligible
            ][:5],
        },
    )
