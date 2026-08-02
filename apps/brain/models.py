"""The AI engine is deliberately stateless.

Everything it needs lives in :mod:`apps.network` (world state) and
:mod:`apps.dispatch` (route plans, corridors), and everything it produces is
either returned to the caller or written back to those apps.  Keeping it free
of its own tables means a route can be recomputed from scratch at any moment
and the engine can be scaled horizontally without coordination.

This module exists so Django's app registry has a models module to import.
"""
