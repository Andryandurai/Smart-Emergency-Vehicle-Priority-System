"""Training with an honest accept/reject rule.

Every trainer reports its validation error against the statistical baseline it
would replace, and :func:`should_deploy` refuses a model that does not beat it
by a clear margin. A model that ties its baseline adds a dependency, a failure
mode and an explanation surface for nothing.

Training data comes from the platform's own history - observations, completed
trips, the preemption audit trail. Where there is not enough of it,
``--synthetic`` generates labelled examples from the same statistical
relationships the baselines encode. That is useful for exercising the pipeline
end to end; it is **not** useful for accuracy, and a model trained that way is
tagged ``synthetic`` in its metadata so nobody mistakes it for one trained on
real operations.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

log = logging.getLogger("sevps.ml.training")

#: A model must beat its baseline by this much to be worth deploying.
MIN_IMPROVEMENT = 0.05


@dataclass
class TrainingResult:
    estimator: str
    samples: int
    model_error: float
    baseline_error: float
    metric: str
    metadata: dict
    synthetic: bool = False

    @property
    def improvement(self) -> float:
        """Fractional error reduction versus the baseline."""
        if not self.baseline_error:
            return 0.0
        return (self.baseline_error - self.model_error) / self.baseline_error

    @property
    def should_deploy(self) -> bool:
        return self.improvement >= MIN_IMPROVEMENT

    def report(self) -> str:
        verdict = (
            f"DEPLOY  ({self.improvement:+.1%} better than baseline)"
            if self.should_deploy
            else f"REJECT  ({self.improvement:+.1%} - not enough to justify the model)"
        )
        return (
            f"  {self.estimator:20} {self.samples:>6} samples  "
            f"{self.metric} model={self.model_error:.4f} baseline={self.baseline_error:.4f}  {verdict}"
        )


def _split(features, targets, baselines):
    """Split features, targets and baseline predictions *together*.

    Splitting them separately would compare the model's test-set error against
    the baseline's error on a different set of rows, which is not a comparison
    at all. One call, one shuffle, three aligned arrays.
    """
    from sklearn.model_selection import train_test_split

    return train_test_split(features, targets, baselines, test_size=0.2, random_state=42)


def _regressor():
    from sklearn.ensemble import HistGradientBoostingRegressor

    return HistGradientBoostingRegressor(
        max_iter=250, learning_rate=0.08, max_depth=6, random_state=42
    )


def _classifier():
    from sklearn.ensemble import HistGradientBoostingClassifier

    return HistGradientBoostingClassifier(
        max_iter=250, learning_rate=0.08, max_depth=6, random_state=42
    )


def _train_regression(estimator, rows, targets, baseline_predictions, synthetic=False):
    import numpy as np
    from sklearn.metrics import mean_absolute_error

    X = np.asarray(rows, dtype=float)
    y = np.asarray(targets, dtype=float)
    baseline = np.asarray(baseline_predictions, dtype=float)

    X_train, X_test, y_train, y_test, _, baseline_test = _split(X, y, baseline)

    model = _regressor()
    model.fit(X_train, y_train)

    # Both errors on the same held-out rows, or the comparison is meaningless.
    model_error = mean_absolute_error(y_test, model.predict(X_test))
    baseline_error = mean_absolute_error(y_test, baseline_test)

    metadata = {
        "trained_at": timezone.now().isoformat(),
        "samples": len(y),
        "features": list(estimator.feature_names),
        "validation_mae": float(model_error),
        "baseline_mae": float(baseline_error),
        "target_std": float(np.std(y)),
        "synthetic": synthetic,
    }
    return model, TrainingResult(
        estimator=estimator.name, samples=len(y), model_error=float(model_error),
        baseline_error=float(baseline_error), metric="MAE", metadata=metadata,
        synthetic=synthetic,
    )


def _train_classification(estimator, rows, targets, baseline_predictions, synthetic=False):
    import numpy as np
    from sklearn.metrics import accuracy_score

    X = np.asarray(rows, dtype=float)
    y = np.asarray(targets)
    baseline = np.asarray(baseline_predictions)

    X_train, X_test, y_train, y_test, _, baseline_test = _split(X, y, baseline)

    model = _classifier()
    model.fit(X_train, y_train)

    # Errors, so lower is better and the comparison reads the same way as MAE.
    model_error = 1.0 - accuracy_score(y_test, model.predict(X_test))
    baseline_error = 1.0 - accuracy_score(y_test, baseline_test)

    metadata = {
        "trained_at": timezone.now().isoformat(),
        "samples": len(y),
        "features": list(estimator.feature_names),
        "validation_accuracy": float(1.0 - model_error),
        "baseline_accuracy": float(1.0 - baseline_error),
        "classes": sorted({int(v) for v in np.unique(y)}),
        "synthetic": synthetic,
    }
    return model, TrainingResult(
        estimator=estimator.name, samples=len(y), model_error=float(model_error),
        baseline_error=float(baseline_error), metric="error-rate", metadata=metadata,
        synthetic=synthetic,
    )


# ---------------------------------------------------------------------------
# Dataset builders
# ---------------------------------------------------------------------------
def congestion_dataset(days: int = 60, horizon_min: int = 15):
    """(features at t, speed factor at t + horizon) from observation history."""
    from apps.brain.ml import estimators as est
    from apps.network.models import RoadSegment, TrafficObservation, TrafficProfile

    since = timezone.now() - timezone.timedelta(days=days)
    segments = {
        s.id: s for s in RoadSegment.objects.only("id", "free_flow_kmh", "road_class", "lanes")
    }
    profiles = {
        (p.segment_id, p.weekday, p.hour): p.speed_factor
        for p in TrafficProfile.objects.only("segment_id", "weekday", "hour", "speed_factor")
    }

    by_segment: dict[int, list] = {}
    for observation in (
        TrafficObservation.objects.filter(observed_at__gte=since)
        .only("segment_id", "observed_at", "speed_kmh")
        .order_by("segment_id", "observed_at")
        .iterator(chunk_size=5000)
    ):
        by_segment.setdefault(observation.segment_id, []).append(observation)

    from apps.brain.congestion import default_speed_factor

    horizon = timezone.timedelta(minutes=horizon_min)
    rows, targets, baselines = [], [], []

    for segment_id, observations in by_segment.items():
        segment = segments.get(segment_id)
        if segment is None:
            continue
        design = segment.design_speed_kmh
        for index, current in enumerate(observations):
            future = _future_observation(observations, index, current.observed_at + horizon)
            if future is None:
                continue
            local = timezone.localtime(current.observed_at)
            historical = profiles.get(
                (segment_id, local.weekday(), local.hour), default_speed_factor(local)
            )
            live = min(1.3, current.speed_kmh / design)

            features = {
                "weekday": local.weekday(), "hour": local.hour, "minute": local.minute,
                "road_class_index": est.road_class_index(segment.road_class),
                "design_kmh": design, "lanes": segment.lanes,
                "live_factor": live, "horizon_min": horizon_min,
                "historical_factor": historical,
            }
            rows.append(est.CONGESTION.vectorise(features))
            targets.append(min(1.3, future.speed_kmh / design))
            baselines.append(est.CONGESTION.baseline(features)[0])

    return rows, targets, baselines


def _future_observation(observations, start: int, at):
    for row in observations[start + 1:]:
        if row.observed_at >= at:
            return row if (row.observed_at - at).total_seconds() <= 600 else None
    return None


def eta_dataset(days: int = 90):
    """(route features, actual - planned) from completed trips."""
    from apps.brain.ml import estimators as est
    from apps.dispatch.models import EmergencyTrip

    since = timezone.now() - timezone.timedelta(days=days)
    rows, targets, baselines = [], [], []

    trips = (
        EmergencyTrip.objects.filter(
            created_at__gte=since,
            departed_scene_at__isnull=False,
            arrived_hospital_at__isnull=False,
        )
        .select_related("vehicle")
        .prefetch_related("routes")
    )
    for trip in trips:
        plan = trip.routes.filter(is_active=False).order_by("computed_at").first() or trip.active_route
        if plan is None or not plan.total_duration_s:
            continue
        actual = trip.transport_time_s
        if actual is None:
            continue

        steps = plan.steps or []
        speeds = [float(s.get("predicted_speed_kmh", 0) or 0) for s in steps]
        mean_congestion = 1.0 - (sum(speeds) / len(speeds)) / 50.0 if speeds else 0.5
        local = timezone.localtime(trip.departed_scene_at)

        features = {
            "weekday": local.weekday(), "hour": local.hour, "minute": local.minute,
            "planned_duration_s": plan.total_duration_s,
            "planned_distance_m": plan.total_distance_m,
            "signalised_count": sum(1 for s in steps if s.get("is_signalised_exit")),
            "priority_level": trip.priority_level,
            "mean_congestion": max(0.0, min(1.0, mean_congestion)),
            "vehicle_speed_kmh": trip.vehicle.speed_kmh,
            "completion": 1.0,
        }
        rows.append(est.ETA_RESIDUAL.vectorise(features))
        targets.append(actual - plan.total_duration_s)
        baselines.append(0.0)      # the baseline is "no correction"

    return rows, targets, baselines


def corridor_dataset(days: int = 90):
    """(request features, granted?) from the preemption audit trail."""
    from apps.brain.ml import estimators as est
    from apps.dispatch.models import SignalPreemption

    since = timezone.now() - timezone.timedelta(days=days)
    rows, targets, baselines = [], [], []

    for preemption in (
        SignalPreemption.objects.filter(created_at__gte=since)
        .select_related("signal", "trip")
        .iterator(chunk_size=2000)
    ):
        local = timezone.localtime(preemption.created_at)
        seconds_away = (
            (preemption.predicted_arrival_at - preemption.created_at).total_seconds()
            if preemption.predicted_arrival_at
            else 60.0
        )
        features = {
            "weekday": local.weekday(), "hour": local.hour, "minute": local.minute,
            "seconds_to_arrival": seconds_away,
            "congestion_index": 0.0,
            "lanes": 2,
            "priority_level": preemption.trip.priority_level,
            "supports_preemption": int(preemption.signal.supports_preemption),
            "recent_preemptions": 0,
            "min_recovery_s": preemption.signal.min_recovery_s,
            "is_online": int(preemption.signal.is_online),
        }
        rows.append(est.CORRIDOR_SUCCESS.vectorise(features))
        targets.append(1 if preemption.activated_at else 0)
        baselines.append(1 if est.CORRIDOR_SUCCESS.baseline(features)[0] >= 0.5 else 0)

    return rows, targets, baselines


def priority_dataset(days: int = 180):
    """(clinical picture, level actually run) from the Layer 6 directive log."""
    from apps.brain.ml import estimators as est
    from apps.brain.ml.services import CATEGORY_INDEX
    from apps.dispatch.models import PriorityDirective
    from apps.hospitals.rules import resolve_rule

    since = timezone.now() - timezone.timedelta(days=days)
    rows, targets, baselines = [], [], []

    for directive in (
        PriorityDirective.objects.filter(created_at__gte=since)
        .select_related("trip")
        .iterator(chunk_size=2000)
    ):
        trip = directive.trip
        rule = resolve_rule(trip.emergency_category)
        local = timezone.localtime(directive.created_at)
        features = {
            "weekday": local.weekday(), "hour": local.hour, "minute": local.minute,
            "category_index": CATEGORY_INDEX.get(trip.emergency_category, 10),
            "patient_age": trip.patient_age if trip.patient_age is not None else 45,
            "deteriorating": int(trip.patient_deteriorating),
            "requires_icu": int(rule.requires_icu),
            "golden_window_min": rule.golden_window_min or 0,
            "time_critical": int(rule.time_critical),
            "is_transfer": int(trip.emergency_category == "transfer"),
        }
        rows.append(est.EMERGENCY_PRIORITY.vectorise(features))
        targets.append(int(directive.priority_level))
        baselines.append(int(rule.priority_level))

    return rows, targets, baselines


# ---------------------------------------------------------------------------
# Synthetic bootstrap
# ---------------------------------------------------------------------------
def synthetic_congestion(count: int = 4000, seed: int = 42):
    """Labelled examples from the relationships the baseline already encodes,
    plus structure the baseline cannot express (a school-run spike, a lane
    effect). Exercises the pipeline; proves nothing about real accuracy."""
    import random

    from apps.brain.ml import estimators as est

    rng = random.Random(seed)
    rows, targets, baselines = [], [], []

    for _ in range(count):
        weekday = rng.randrange(7)
        hour = rng.randrange(24)
        minute = rng.randrange(60)
        lanes = rng.choice([1, 2, 2, 3, 4])
        design = rng.choice([25.0, 35.0, 40.0, 50.0, 60.0])
        horizon = rng.choice([5, 10, 15, 20, 30])

        peak = max(
            0.0,
            1.0 - min(abs(hour - 9), abs(hour - 18.5)) / 3.0,
        ) * (0.55 if weekday < 5 else 0.2)
        true_factor = max(0.15, min(1.1, 1.0 - peak + rng.gauss(0, 0.06)))
        # Structure a blend of live+historical cannot represent: narrow lanes
        # congest disproportionately at the peak.
        if lanes <= 2 and peak > 0.3:
            true_factor *= 0.85

        live = max(0.1, min(1.2, true_factor + rng.gauss(0, 0.12)))
        historical = max(0.15, min(1.1, 1.0 - peak * 0.8))

        features = {
            "weekday": weekday, "hour": hour, "minute": minute,
            "road_class_index": rng.randrange(7), "design_kmh": design,
            "lanes": lanes, "live_factor": live, "horizon_min": horizon,
            "historical_factor": historical,
        }
        rows.append(est.CONGESTION.vectorise(features))
        targets.append(true_factor)
        baselines.append(est.CONGESTION.baseline(features)[0])

    return rows, targets, baselines


def synthetic_corridor(count: int = 3000, seed: int = 7):
    import random

    from apps.brain.ml import estimators as est

    rng = random.Random(seed)
    rows, targets, baselines = [], [], []

    for _ in range(count):
        seconds_away = rng.uniform(5, 180)
        congestion = rng.random()
        lanes = rng.choice([1, 2, 3, 4])
        priority = rng.choice([1, 1, 2, 2, 3])
        recent = rng.choices([0, 1, 2, 3], weights=[0.6, 0.25, 0.1, 0.05])[0]
        online = rng.random() > 0.05
        supports = rng.random() > 0.08

        # Too little warning, a busy junction or a recent hold all reduce it.
        score = (
            0.9
            - 0.35 * max(0.0, (25 - seconds_away) / 25)
            - 0.25 * congestion
            - 0.18 * recent
            + 0.08 * (4 - priority)
        )
        granted = int(online and supports and rng.random() < max(0.02, min(0.98, score)))

        features = {
            "weekday": rng.randrange(7), "hour": rng.randrange(24), "minute": rng.randrange(60),
            "seconds_to_arrival": seconds_away, "congestion_index": congestion,
            "lanes": lanes, "priority_level": priority,
            "supports_preemption": int(supports), "recent_preemptions": recent,
            "min_recovery_s": rng.choice([30, 45, 60]), "is_online": int(online),
        }
        rows.append(est.CORRIDOR_SUCCESS.vectorise(features))
        targets.append(granted)
        baselines.append(1 if est.CORRIDOR_SUCCESS.baseline(features)[0] >= 0.5 else 0)

    return rows, targets, baselines


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def train(name: str, *, synthetic: bool = False, days: int = 90, min_samples: int = 200):
    """Train one estimator. Returns ``(model, TrainingResult)`` or ``None``."""
    from apps.brain.ml import estimators as est

    estimator = est.get(name)
    if estimator is None:
        raise ValueError(f"unknown estimator {name!r}")

    if name == "congestion":
        data = synthetic_congestion() if synthetic else congestion_dataset(days)
        trainer = _train_regression
    elif name == "eta_residual":
        data = eta_dataset(days)
        trainer = _train_regression
    elif name == "corridor_success":
        data = synthetic_corridor() if synthetic else corridor_dataset(days)
        trainer = _train_classification
    elif name == "emergency_priority":
        data = priority_dataset(days)
        trainer = _train_classification
    else:
        return None       # clearance_time has no labelled source yet

    rows, targets, baselines = data
    if len(rows) < min_samples:
        log.info("%s: only %d samples, need %d", name, len(rows), min_samples)
        return None

    model, result = trainer(estimator, rows, targets, baselines, synthetic=synthetic)
    return model, result
