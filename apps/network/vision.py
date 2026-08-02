"""Backward-compatible facade over the Phase 7 computer-vision package.

The implementation moved to :mod:`apps.network.cv`, which added emergency
vehicle, road-block and illegal-parking detection and put confidence and
evidence on every finding. This module stays because five call sites import
from it - the worker, two management commands, a viewset and a test - and
breaking them to move a file would be churn for its own sake.

New code should import from ``apps.network.cv`` directly.
"""
from __future__ import annotations

from apps.network.cv.backends import backend_name as cv_backend
from apps.network.cv.detection import (  # noqa: F401 - re-exported
    VEHICLE_CLASSES,
    Detection,
    FrameAnalysis,
)
from apps.network.cv.pipeline import analyse_camera, ingest, sweep  # noqa: F401


def ingest_camera_analysis(camera, analysis):
    """Historic name for :func:`apps.network.cv.pipeline.ingest`."""
    return ingest(camera, analysis)


__all__ = [
    "cv_backend",
    "analyse_camera",
    "ingest",
    "ingest_camera_analysis",
    "sweep",
    "Detection",
    "FrameAnalysis",
    "VEHICLE_CLASSES",
]
