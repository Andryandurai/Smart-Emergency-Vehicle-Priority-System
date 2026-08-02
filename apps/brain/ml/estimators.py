"""The SEVPS estimators.

Five models, each with a statistical baseline that keeps the platform working
before any of them are trained:

======================  ====================================================
congestion              speed factor on a segment N minutes ahead
eta_residual            correction to the router's own ETA
emergency_priority      advisory Layer 6 level from the clinical picture
corridor_success        will this signal preemption actually be granted
clearance_time          how long a junction needs to flush before arrival
======================  ====================================================

All are gradient-boosted trees. That is a deliberate choice, not a default:
every target here is a tabular regression or a small classification over
~10 hand-built features, which is the regime where boosted trees beat neural
networks on accuracy, train in seconds on a laptop, and - decisively for this
platform - admit exact SHAP attributions cheaply enough to explain every
prediction rather than a sampled few.
"""
from __future__ import annotations

import math
from typing import Any

from django.utils import timezone

from apps.brain.ml.base import Explanation, FeatureContribution, SEVPSEstimator
from apps.core.enums import PriorityLevel, RoadClass

_ROAD_CLASS_INDEX = {rc: i for i, rc in enumerate(RoadClass.values)}


# ---------------------------------------------------------------------------
# 1. Congestion
# ---------------------------------------------------------------------------
class CongestionEstimator(SEVPSEstimator):
    """Speed factor (observed / free-flow) at a future moment.

    Predicting a *factor* rather than an absolute speed lets one model serve
    every road class - a 0.6 factor means the same thing on a motorway and a
    residential street.
    """

    name = "congestion"
    unit = "speed_factor"
    feature_names = (
        "weekday", "hour", "minute", "road_class_index", "design_kmh",
        "lanes", "live_factor", "horizon_min", "historical_factor",
    )

    def baseline(self, features: dict) -> tuple[float, Explanation]:
        """Blend the live reading with the learned profile, decaying with horizon.

        This is the Phase 1 statistical predictor, kept as the fallback and as
        the yardstick any trained model has to beat.
        """
        live = features.get("live_factor")
        historical = features.get("historical_factor", 0.7)
        horizon_min = features.get("horizon_min", 0.0)

        if live is None:
            return historical, Explanation(
                method="statistical",
                summary="No fresh observation; using the learned weekday/hour profile.",
                contributions=[
                    FeatureContribution("historical_factor", historical, 1.0,
                                        "learned profile for this weekday and hour")
                ],
            )

        weight = math.exp(-max(0.0, horizon_min) / 10.0)
        value = live * weight + historical * (1 - weight)
        return value, Explanation(
            method="statistical",
            summary=(
                f"Blended live observation ({weight:.0%}) with the learned "
                f"profile ({1 - weight:.0%}); confidence in a live reading "
                f"decays over a {horizon_min:.0f} minute horizon."
            ),
            contributions=[
                FeatureContribution("live_factor", live, weight, "current observed speed"),
                FeatureContribution("historical_factor", historical, 1 - weight,
                                    "learned profile for this time"),
            ],
        )

    def postprocess(self, raw, features: dict) -> float:
        return float(max(0.05, min(1.25, raw)))

    def describe_feature(self, name: str, value: Any, contribution: float) -> str:
        direction = "faster" if contribution >= 0 else "slower"
        phrases = {
            "hour": f"time of day ({int(value or 0):02d}:00) implies {direction} traffic",
            "weekday": f"day of week implies {direction} traffic",
            "live_factor": f"current speed reading pushes the forecast {direction}",
            "historical_factor": f"this road's usual pattern pushes it {direction}",
            "horizon_min": f"predicting {value} min ahead makes it {direction}",
            "road_class_index": f"road class implies {direction} flow",
            "lanes": f"{value} lanes implies {direction} flow",
        }
        return phrases.get(name, super().describe_feature(name, value, contribution))


# ---------------------------------------------------------------------------
# 2. ETA residual
# ---------------------------------------------------------------------------
class ETAResidualEstimator(SEVPSEstimator):
    """Correction to the router's own ETA, in seconds.

    Modelling the *residual* rather than the ETA itself matters. The router
    already encodes distance, speed limits, signal delay and the congestion
    forecast; a model asked to reproduce all that from scratch would spend its
    capacity relearning geometry. Asked only where the router is
    systematically wrong - junctions that always take longer, a fleet that
    consistently beats its estimate - it can learn that from far less data.

    A positive residual means the trip took longer than the router predicted.
    """

    name = "eta_residual"
    unit = "seconds"
    feature_names = (
        "planned_duration_s", "planned_distance_m", "signalised_count",
        "priority_level", "hour", "weekday", "mean_congestion",
        "vehicle_speed_kmh", "completion",
    )

    def baseline(self, features: dict) -> tuple[float, Explanation]:
        """No correction, stated plainly.

        Zero is the right default: the router's estimate is the best available
        answer until evidence says otherwise, and inventing a fudge factor
        would be worse than admitting there is nothing to add.
        """
        return 0.0, Explanation(
            method="statistical",
            summary="No trained residual model; the router's own ETA is used unadjusted.",
        )

    def postprocess(self, raw, features: dict) -> float:
        # Cap the correction: a residual model confidently rewriting an ETA by
        # ten minutes is far more likely to be broken than insightful.
        planned = features.get("planned_duration_s", 0.0) or 0.0
        limit = max(60.0, planned * 0.5)
        return float(max(-limit, min(limit, raw)))

    def describe_feature(self, name: str, value: Any, contribution: float) -> str:
        direction = "slower" if contribution >= 0 else "faster"
        phrases = {
            "signalised_count": f"{value} signalised junctions on the route imply {direction}",
            "mean_congestion": f"average congestion on the route implies {direction}",
            "priority_level": f"priority level {value} implies {direction}",
            "hour": f"departing at {int(value or 0):02d}:00 implies {direction}",
            "vehicle_speed_kmh": f"current speed {value} km/h implies {direction}",
        }
        return phrases.get(name, super().describe_feature(name, value, contribution))


# ---------------------------------------------------------------------------
# 3. Emergency priority (advisory only)
# ---------------------------------------------------------------------------
class EmergencyPriorityEstimator(SEVPSEstimator):
    """Advisory Layer 6 priority level from the clinical picture.

    **This model never sets a priority level.** The Rule-Based Emergency
    Engine does, because the mapping from presentation to urgency is a
    clinical governance decision that must be inspectable, versioned and
    signed off - not learned from whatever the last six months happened to
    contain. Learning it would also bake in historical under-triage of exactly
    the populations most likely to have been under-triaged.

    What it is for: flagging *disagreement*. When the model strongly expects a
    different level than the rule produced, that is worth a dispatcher's
    attention - it usually means the recorded category does not match the rest
    of the clinical picture.
    """

    name = "emergency_priority"
    unit = "priority_level"
    is_classifier = True
    feature_names = (
        "category_index", "patient_age", "deteriorating", "hour",
        "requires_icu", "golden_window_min", "time_critical", "is_transfer",
    )

    #: Model classes are 0-indexed; priority levels are 1..4.
    def postprocess(self, raw, features: dict) -> int:
        return int(max(1, min(4, int(raw))))

    def baseline(self, features: dict) -> tuple[int, Explanation]:
        """The rule engine's own answer - which is also the authority."""
        level = int(features.get("rule_level", PriorityLevel.MODERATE))
        return level, Explanation(
            method="rule",
            summary=(
                "From the Rule-Based Emergency Engine. The rule base is the "
                "authority for priority; the model only ever advises."
            ),
            contributions=[
                FeatureContribution(
                    "emergency_category", features.get("category"), 1.0,
                    "clinical category selected by the crew",
                )
            ],
        )

    def describe_feature(self, name: str, value: Any, contribution: float) -> str:
        direction = "toward higher priority" if contribution >= 0 else "toward lower priority"
        phrases = {
            "deteriorating": f"deterioration flag ({bool(value)}) pushes {direction}",
            "patient_age": f"patient age {value} pushes {direction}",
            "requires_icu": f"ICU requirement pushes {direction}",
            "time_critical": f"time-critical presentation pushes {direction}",
            "golden_window_min": f"a {value} minute clinical window pushes {direction}",
        }
        return phrases.get(name, super().describe_feature(name, value, contribution))


# ---------------------------------------------------------------------------
# 4. Green corridor success
# ---------------------------------------------------------------------------
class CorridorSuccessEstimator(SEVPSEstimator):
    """Probability that a preemption request will actually be granted.

    Useful before the request is made: a junction the platform expects to fail
    should be routed around, not discovered at the stop line. Learned from the
    Phase 3 preemption audit trail, where every request records whether it
    activated, was yielded, or failed.
    """

    name = "corridor_success"
    unit = "probability"
    is_classifier = True
    feature_names = (
        "seconds_to_arrival", "congestion_index", "lanes", "hour",
        "priority_level", "supports_preemption", "recent_preemptions",
        "min_recovery_s", "is_online",
    )

    def baseline(self, features: dict) -> tuple[float, Explanation]:
        """The deterministic guard rails, expressed as a probability."""
        if not features.get("is_online", 1):
            return 0.0, Explanation(
                method="rule", summary="Controller is offline; preemption cannot succeed."
            )
        if not features.get("supports_preemption", 1):
            return 0.0, Explanation(
                method="rule", summary="Controller does not support preemption."
            )
        recent = features.get("recent_preemptions", 0)
        probability = 0.9 if recent == 0 else max(0.3, 0.9 - 0.2 * recent)
        return probability, Explanation(
            method="statistical",
            summary=(
                "From the controller's capability and recovery window; "
                f"{recent} recent preemption(s) at this junction."
            ),
            contributions=[
                FeatureContribution("recent_preemptions", recent, -0.2 * recent,
                                    "recovery window reduces the chance of another green")
            ],
        )

    def postprocess(self, raw, features: dict) -> float:
        return float(max(0.0, min(1.0, raw)))

    def _raw_predict(self, model, row):
        import numpy as np

        # For a binary classifier the useful output is P(success), not the
        # hard label - the caller wants to weigh it, not be told yes or no.
        if hasattr(model, "predict_proba"):
            return float(model.predict_proba(np.asarray([row], dtype=float))[0][1])
        return float(model.predict(np.asarray([row], dtype=float))[0])

    def describe_feature(self, name: str, value: Any, contribution: float) -> str:
        direction = "more likely" if contribution >= 0 else "less likely"
        phrases = {
            "seconds_to_arrival": f"{value}s of warning makes a green {direction}",
            "congestion_index": f"junction congestion makes it {direction}",
            "recent_preemptions": f"{value} recent hold(s) here make it {direction}",
            "priority_level": f"priority level {value} makes it {direction}",
        }
        return phrases.get(name, super().describe_feature(name, value, contribution))


# ---------------------------------------------------------------------------
# 5. Clearance time
# ---------------------------------------------------------------------------
class ClearanceTimeEstimator(SEVPSEstimator):
    """Seconds of green needed before arrival to flush the queue ahead.

    Currently a hand-tuned function of congestion and lane count. Getting it
    wrong is expensive in both directions: too short and the ambulance meets a
    queue that has not moved; too long and cross traffic is held for no reason,
    which at a busy junction can spill back and block the corridor itself.
    """

    name = "clearance_time"
    unit = "seconds"
    feature_names = (
        "congestion_index", "lanes", "hour", "weekday",
        "road_class_index", "design_kmh", "priority_level",
    )

    BASE_CLEARANCE_S = 8.0
    MAX_CLEARANCE_S = 35.0

    def baseline(self, features: dict) -> tuple[float, Explanation]:
        congestion = features.get("congestion_index", 0.0)
        lanes = features.get("lanes", 2)
        queue_factor = congestion * (1.0 + 0.25 * max(0, lanes - 1))
        value = min(self.MAX_CLEARANCE_S, self.BASE_CLEARANCE_S + 40.0 * queue_factor)
        return value, Explanation(
            method="statistical",
            summary=(
                f"{self.BASE_CLEARANCE_S:.0f}s base plus a queue allowance from "
                f"{congestion:.0%} congestion across {lanes} lane(s)."
            ),
            contributions=[
                FeatureContribution("congestion_index", congestion, 40.0 * queue_factor,
                                    "more queued vehicles need longer to clear"),
                FeatureContribution("lanes", lanes, 0.25 * max(0, lanes - 1),
                                    "more lanes means more vehicles to move"),
            ],
        )

    def postprocess(self, raw, features: dict) -> float:
        return float(max(3.0, min(self.MAX_CLEARANCE_S, raw)))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
CONGESTION = CongestionEstimator()
ETA_RESIDUAL = ETAResidualEstimator()
EMERGENCY_PRIORITY = EmergencyPriorityEstimator()
CORRIDOR_SUCCESS = CorridorSuccessEstimator()
CLEARANCE_TIME = ClearanceTimeEstimator()

REGISTRY: dict[str, SEVPSEstimator] = {
    estimator.name: estimator
    for estimator in (CONGESTION, ETA_RESIDUAL, EMERGENCY_PRIORITY,
                      CORRIDOR_SUCCESS, CLEARANCE_TIME)
}


def get(name: str) -> SEVPSEstimator | None:
    return REGISTRY.get(name)


def registry_status() -> list[dict]:
    return [estimator.status() for estimator in REGISTRY.values()]


def time_features(when=None) -> dict:
    when = when or timezone.localtime()
    return {"weekday": when.weekday(), "hour": when.hour, "minute": when.minute}


def road_class_index(road_class: str) -> int:
    return _ROAD_CLASS_INDEX.get(road_class, 3)
