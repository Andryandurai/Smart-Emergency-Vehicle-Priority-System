"""The prediction envelope and the estimator base class.

The envelope is the contract the whole ML subsystem is built around: a caller
receives a value, a calibrated confidence and a per-feature explanation, or it
receives nothing. There is no code path that returns a bare number.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from django.conf import settings

log = logging.getLogger("sevps.ml")


@dataclass(frozen=True)
class FeatureContribution:
    """One input's signed effect on this prediction."""

    name: str
    value: Any
    #: Signed contribution in the model's output units. Positive pushes the
    #: prediction up, negative pulls it down.
    contribution: float
    #: Human-readable statement of what this feature did, for a UI that has
    #: no room to render a SHAP plot.
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "contribution": round(self.contribution, 4),
            "detail": self.detail,
        }


@dataclass
class Explanation:
    """Why the model produced this value.

    ``method`` distinguishes a real attribution from a fallback: SHAP when the
    explainer is available, otherwise the model's global feature importances
    or the rule that fired. A caller must be able to tell an exact per-instance
    attribution from a rough global one.
    """

    method: str
    contributions: list[FeatureContribution] = field(default_factory=list)
    #: Model output before any feature moved it - SHAP's expected value.
    baseline: float | None = None
    summary: str = ""

    def top(self, count: int = 3) -> list[FeatureContribution]:
        return sorted(self.contributions, key=lambda c: -abs(c.contribution))[:count]

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "baseline": round(self.baseline, 4) if self.baseline is not None else None,
            "summary": self.summary or self.narrate(),
            "contributions": [c.as_dict() for c in self.top(6)],
        }

    def narrate(self) -> str:
        """One sentence naming the features that actually moved the answer."""
        drivers = self.top(3)
        if not drivers:
            return "No individual feature dominated this prediction."
        parts = [
            f"{c.name.replace('_', ' ')} ({'+' if c.contribution >= 0 else ''}"
            f"{c.contribution:.2f})"
            for c in drivers
        ]
        return "Driven mainly by " + ", ".join(parts) + "."


@dataclass
class Prediction:
    """A model output that cannot be used without its caveats.

    ``confidence`` is in [0, 1] and is meant to be acted on: the API and the
    console both surface low-confidence predictions differently, and
    :meth:`is_actionable` is the single place the threshold lives.
    """

    #: What the model predicts. Type depends on the model.
    value: Any
    confidence: float
    explanation: Explanation
    model: str
    #: "model" when a trained estimator produced this, "baseline" when the
    #: statistical fallback did. Callers and dashboards must be able to tell.
    source: str = "model"
    unit: str = ""
    #: Set when a model disagrees with the authoritative rule. Never acted on
    #: automatically - surfaced for a human.
    disagreement: str = ""
    context: dict = field(default_factory=dict)

    #: Below this a prediction is advisory only and is rendered as such.
    ACTIONABLE_CONFIDENCE = 0.6

    def __post_init__(self):
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

    @property
    def is_actionable(self) -> bool:
        return self.confidence >= self.ACTIONABLE_CONFIDENCE

    def as_dict(self) -> dict:
        return {
            "prediction": self.value,
            "confidence": round(self.confidence, 4),
            "is_actionable": self.is_actionable,
            "explanation": self.explanation.as_dict(),
            "model": self.model,
            "source": self.source,
            "unit": self.unit,
            "disagreement": self.disagreement,
            "context": self.context,
        }


# ---------------------------------------------------------------------------
# Estimator base
# ---------------------------------------------------------------------------
class SEVPSEstimator:
    """A trained model plus the machinery that makes its output usable.

    Subclasses declare ``name``, ``feature_names`` and ``unit``, and implement
    :meth:`baseline` - the statistical answer used when no model is loaded and
    as the yardstick a trained model must beat to be worth deploying.
    """

    name: str = "estimator"
    feature_names: Sequence[str] = ()
    unit: str = ""
    #: Regression by default; classifiers override to report probabilities.
    is_classifier: bool = False

    def __init__(self):
        self._model = None
        self._explainer = None
        self._metadata: dict = {}
        self._load_attempted = False

    # -- persistence --------------------------------------------------------
    @property
    def model_path(self) -> Path:
        root = settings.SEVPS.get("MODEL_DIR") or (settings.BASE_DIR / "models")
        return Path(root) / f"{self.name}.joblib"

    def load(self):
        """Load the trained model once, tolerating its absence."""
        if self._load_attempted:
            return self._model
        self._load_attempted = True

        path = self.model_path
        if not path.exists():
            return None
        try:
            import joblib

            bundle = joblib.load(path)
            self._model = bundle["model"]
            self._metadata = bundle.get("metadata", {})
            log.info("loaded %s from %s", self.name, path)
        except Exception:
            log.warning("could not load model %s at %s", self.name, path, exc_info=True)
            self._model = None
        return self._model

    def save(self, model, metadata: dict) -> Path:
        import joblib

        path = self.model_path
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump({"model": model, "metadata": metadata}, path)
        return path

    @property
    def metadata(self) -> dict:
        self.load()
        return self._metadata

    def is_available(self) -> bool:
        return self.load() is not None

    # -- prediction ---------------------------------------------------------
    def baseline(self, features: dict) -> tuple[Any, Explanation]:
        """Statistical answer used when no model is loaded.

        Must always work - it is what keeps SEVPS running on day one, before
        any history exists to train on.
        """
        raise NotImplementedError

    def predict(self, features: dict) -> Prediction:
        model = self.load()
        if model is None:
            value, explanation = self.baseline(features)
            return Prediction(
                value=value,
                # A rule-derived answer is dependable but not precise; saying
                # so honestly is better than claiming certainty it lacks.
                confidence=0.5,
                explanation=explanation,
                model=self.name,
                source="baseline",
                unit=self.unit,
                context={"reason": "no trained model available"},
            )

        row = self.vectorise(features)
        raw = self._raw_predict(model, row)
        confidence = self.confidence_for(model, row, raw)
        explanation = self.explain(model, row, features)
        return Prediction(
            value=self.postprocess(raw, features),
            confidence=confidence,
            explanation=explanation,
            model=self.name,
            source="model",
            unit=self.unit,
            context={"trained_at": self._metadata.get("trained_at")},
        )

    def vectorise(self, features: dict) -> list[float]:
        """Feature dict -> ordered vector. Order must match training exactly."""
        return [float(features.get(name, 0.0) or 0.0) for name in self.feature_names]

    def _raw_predict(self, model, row: list[float]):
        import numpy as np

        return model.predict(np.asarray([row], dtype=float))[0]

    def postprocess(self, raw, features: dict):
        return float(raw)

    def confidence_for(self, model, row: list[float], raw) -> float:
        """How much to trust this specific prediction.

        For a classifier, the predicted class probability. For a regressor,
        derived from the residual spread measured at training time - a model
        whose validation error was large should not report high confidence
        just because it is a model.
        """
        import numpy as np

        if self.is_classifier and hasattr(model, "predict_proba"):
            proba = model.predict_proba(np.asarray([row], dtype=float))[0]
            return float(max(proba))

        mae = self._metadata.get("validation_mae")
        spread = self._metadata.get("target_std")
        if not mae or not spread:
            return 0.65  # trained, but unquantified - deliberately middling

        # Error small relative to the natural spread of the target means the
        # model is explaining real structure rather than echoing the mean.
        ratio = mae / spread
        return float(max(0.05, min(0.99, math.exp(-1.5 * ratio))))

    # -- explanation --------------------------------------------------------
    def explainer(self):
        """A SHAP TreeExplainer, built lazily and cached.

        Tree models get exact per-instance attributions in microseconds, which
        is what makes it affordable to explain *every* prediction rather than
        offering explanation as a separate, slower endpoint.
        """
        if self._explainer is not None:
            return self._explainer
        model = self.load()
        if model is None:
            return None
        try:
            import shap

            self._explainer = shap.TreeExplainer(model)
        except Exception:
            log.info("SHAP unavailable for %s; using feature importances", self.name)
            self._explainer = False
        return self._explainer or None

    def explain(self, model, row: list[float], features: dict) -> Explanation:
        explainer = self.explainer()
        if explainer is not None:
            try:
                return self._shap_explanation(explainer, row, features)
            except Exception:
                log.warning("SHAP failed for %s; falling back", self.name, exc_info=True)
        return self._importance_explanation(model, row, features)

    def _shap_explanation(self, explainer, row: list[float], features: dict) -> Explanation:
        import numpy as np

        values = explainer.shap_values(np.asarray([row], dtype=float))
        if isinstance(values, list):        # multiclass: explain the argmax
            model = self.load()
            index = int(np.argmax(model.predict_proba(np.asarray([row], dtype=float))[0]))
            contributions = np.asarray(values[index])[0]
            baseline = float(np.asarray(explainer.expected_value)[index])
        else:
            contributions = np.asarray(values)[0]
            expected = explainer.expected_value
            baseline = float(np.asarray(expected).flatten()[0])

        return Explanation(
            method="shap",
            baseline=baseline,
            contributions=[
                FeatureContribution(
                    name=name,
                    value=features.get(name),
                    contribution=float(contributions[index]),
                    detail=self.describe_feature(name, features.get(name), float(contributions[index])),
                )
                for index, name in enumerate(self.feature_names)
                if index < len(contributions)
            ],
        )

    def _importance_explanation(self, model, row: list[float], features: dict) -> Explanation:
        """Global importances - honest about being less precise than SHAP."""
        importances = getattr(model, "feature_importances_", None)
        if importances is None:
            return Explanation(
                method="unavailable",
                summary="This model does not expose feature attributions.",
            )
        return Explanation(
            method="feature_importance",
            summary=(
                "Global feature importances, not a per-prediction attribution: "
                "these describe the model overall rather than this case."
            ),
            contributions=[
                FeatureContribution(
                    name=name,
                    value=features.get(name),
                    contribution=float(importances[index]),
                )
                for index, name in enumerate(self.feature_names)
                if index < len(importances)
            ],
        )

    def describe_feature(self, name: str, value: Any, contribution: float) -> str:
        """Subclasses override to phrase a contribution in domain terms."""
        direction = "raised" if contribution >= 0 else "lowered"
        return f"{name.replace('_', ' ')} = {value} {direction} the estimate"

    # -- reporting ----------------------------------------------------------
    def status(self) -> dict:
        available = self.is_available()
        return {
            "name": self.name,
            "available": available,
            "unit": self.unit,
            "path": str(self.model_path),
            "features": list(self.feature_names),
            "metadata": self._metadata if available else {},
        }
