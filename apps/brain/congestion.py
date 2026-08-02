"""Congestion prediction (features 4.4 / §5 "Machine Learning").

The forecaster answers one question the router asks thousands of times per
route: *what speed will this road be carrying when the ambulance actually
gets there* - not what it is carrying now.  That distinction is what lets
SEVPS route around a jam that has not formed yet.

Two backends:

``statistical``  built in, no dependencies.  Blends the live observed speed
                 with the learned weekday/hour profile for the segment, with
                 the blend weight decaying as the prediction horizon grows,
                 plus an explicit penalty term for active road events.
``sklearn``      a gradient-boosting regressor trained by
                 ``manage.py train_congestion_model`` and loaded from
                 ``SEVPS_CONGESTION_MODEL_PATH``.  Predicts the same quantity
                 (speed factor), so it drops straight in.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone

from apps.core.enums import RoadClass

log = logging.getLogger("sevps.brain.congestion")

#: Horizon at which the live observation has decayed to 1/e of its influence.
LIVE_DECAY_TAU_S = 600.0
#: A live speed older than this is not trusted at all.
LIVE_MAX_AGE_S = 900.0

_sk_model = None
_sk_load_attempted = False

_ROAD_CLASS_INDEX = {rc: i for i, rc in enumerate(RoadClass.values)}


def predictor_backend() -> str:
    """Which congestion backend this process will actually use."""
    path = settings.SEVPS["CONGESTION_MODEL_PATH"]
    if not path:
        return "statistical"
    if _load_sklearn_model() is None:
        return "statistical (model configured but not loadable)"
    return f"sklearn:{path}"


def _load_sklearn_model():
    global _sk_model, _sk_load_attempted
    if _sk_load_attempted:
        return _sk_model
    _sk_load_attempted = True
    path = settings.SEVPS["CONGESTION_MODEL_PATH"]
    if not path:
        return None
    try:
        import joblib

        _sk_model = joblib.load(path)
        log.info("Loaded congestion model from %s", path)
    except Exception:
        log.warning("Could not load congestion model at %s", path, exc_info=True)
        _sk_model = None
    return _sk_model


# ---------------------------------------------------------------------------
# Default demand curve - used where no history has been learned yet
# ---------------------------------------------------------------------------
def default_speed_factor(when) -> float:
    """Typical speed-to-free-flow ratio for an Indian metro at ``when``.

    A sensible prior so a brand-new deployment routes intelligently on day one,
    before any history exists.  Learned profiles override it per segment.
    """
    minutes = when.hour * 60 + when.minute
    # (peak centre, spread, depth) - morning, evening, lunch
    peaks = ((9 * 60 + 15, 85, 0.45), (18 * 60 + 45, 105, 0.52), (13 * 60, 110, 0.18))
    congestion = 0.10
    for centre, width, depth in peaks:
        congestion += depth * math.exp(-(((minutes - centre) / width) ** 2))
    if when.weekday() >= 5:
        congestion *= 0.65
    if when.hour < 6:
        congestion *= 0.35
    return max(0.15, 1.0 - min(0.85, congestion))


@dataclass
class SegmentForecast:
    segment_id: int
    speed_kmh: float
    speed_factor: float
    horizon_s: float
    basis: str  # live | blended | historical | model

    def as_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "speed_kmh": round(self.speed_kmh, 1),
            "speed_factor": round(self.speed_factor, 3),
            "horizon_s": round(self.horizon_s),
            "basis": self.basis,
        }


@dataclass
class CongestionForecaster:
    """Stateless-per-request predictor bound to a departure time.

    ``profiles`` maps ``(segment_id, weekday, hour)`` to a learned speed
    factor; ``event_penalty`` maps ``segment_id`` to a multiplicative slowdown
    in (0, 1] contributed by active road events.
    """

    departure: object = field(default_factory=timezone.now)
    profiles: dict[tuple[int, int, int], float] = field(default_factory=dict)
    event_penalty: dict[int, float] = field(default_factory=dict)

    def _historical_factor(self, segment_id: int, when) -> tuple[float, bool]:
        """Learned factor for a time, linearly interpolated between hours."""
        weekday, hour = when.weekday(), when.hour
        next_hour = (hour + 1) % 24
        a = self.profiles.get((segment_id, weekday, hour))
        b = self.profiles.get((segment_id, weekday, next_hour))
        if a is None and b is None:
            return default_speed_factor(when), False
        if a is None:
            a = b
        if b is None:
            b = a
        w = when.minute / 60.0
        return a * (1 - w) + b * w, True

    def _live_factor(self, edge) -> float | None:
        """Observed speed ratio, or None when there is no fresh observation."""
        if not edge.live_kmh or edge.live_age_s is None:
            return None
        if edge.live_age_s > LIVE_MAX_AGE_S:
            return None
        return max(0.05, min(1.3, edge.live_kmh / edge.design_kmh))

    def forecast(self, edge, horizon_s: float) -> SegmentForecast:
        """Predicted speed on ``edge`` ``horizon_s`` seconds after departure."""
        when = self.departure + timezone.timedelta(seconds=max(0.0, horizon_s))
        model = _load_sklearn_model()

        if model is not None:
            factor, basis = self._model_factor(model, edge, when, horizon_s)
        else:
            factor, basis = self._statistical_factor(edge, when, horizon_s)

        factor *= self.event_penalty.get(edge.segment_id, 1.0)
        factor = max(0.05, min(1.25, factor))
        return SegmentForecast(
            segment_id=edge.segment_id,
            speed_kmh=max(3.0, edge.design_kmh * factor),
            speed_factor=factor,
            horizon_s=horizon_s,
            basis=basis,
        )

    def _statistical_factor(self, edge, when, horizon_s: float) -> tuple[float, str]:
        historical, learned = self._historical_factor(edge.segment_id, when)
        live = self._live_factor(edge)
        if live is None:
            return historical, "historical" if learned else "prior"

        # Confidence in the live reading decays with both prediction horizon
        # and the age of the observation itself.
        horizon_weight = math.exp(-max(0.0, horizon_s) / LIVE_DECAY_TAU_S)
        freshness = math.exp(-(edge.live_age_s or 0.0) / LIVE_DECAY_TAU_S)
        w = horizon_weight * freshness
        if w > 0.97:
            return live, "live"
        return live * w + historical * (1 - w), "blended"

    def _model_factor(self, model, edge, when, horizon_s: float) -> tuple[float, str]:
        live = self._live_factor(edge)
        historical, _ = self._historical_factor(edge.segment_id, when)
        features = [
            [
                when.weekday(),
                when.hour,
                when.minute,
                _ROAD_CLASS_INDEX.get(edge.road_class, 3),
                edge.design_kmh,
                edge.lanes,
                live if live is not None else historical,
                min(horizon_s, 3600.0) / 60.0,
                historical,
            ]
        ]
        try:
            return float(model.predict(features)[0]), "model"
        except Exception:
            log.warning("congestion model prediction failed; falling back", exc_info=True)
            return self._statistical_factor(edge, when, horizon_s)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------
def load_profiles(segment_ids=None) -> dict[tuple[int, int, int], float]:
    """Load learned weekday/hour profiles keyed for :class:`CongestionForecaster`."""
    from apps.network.models import TrafficProfile

    qs = TrafficProfile.objects.all()
    if segment_ids is not None:
        qs = qs.filter(segment_id__in=segment_ids)
    return {
        (row.segment_id, row.weekday, row.hour): row.speed_factor
        for row in qs.only("segment_id", "weekday", "hour", "speed_factor")
    }


def load_event_penalties() -> dict[int, float]:
    """Multiplicative slowdown per segment from currently active road events.

    Events compound: two concurrent 50%-severity events leave 25% of the
    free-flow speed.  Blocking events are handled separately by the router,
    which removes the edge entirely rather than making it merely slow.
    """
    from apps.network.models import RoadEvent

    penalties: dict[int, float] = {}
    for event in RoadEvent.objects.active().filter(segment__isnull=False):
        # Weight severity by how confident we are the event is real.
        impact = event.severity * max(0.2, event.confidence)
        factor = max(0.05, 1.0 - impact)
        penalties[event.segment_id] = penalties.get(event.segment_id, 1.0) * factor
    return penalties


def blocked_segment_ids() -> set[int]:
    """Segments an emergency vehicle genuinely cannot use."""
    from apps.network.models import RoadEvent, RoadSegment

    blocked = set(
        RoadSegment.objects.filter(is_open=False).values_list("id", flat=True)
    )
    for event in RoadEvent.objects.active().filter(segment__isnull=False):
        if event.blocks_road and event.confidence >= 0.5:
            blocked.add(event.segment_id)
    return blocked


def build_forecaster(departure=None, segment_ids=None) -> CongestionForecaster:
    return CongestionForecaster(
        departure=departure or timezone.now(),
        profiles=load_profiles(segment_ids),
        event_penalty=load_event_penalties(),
    )
