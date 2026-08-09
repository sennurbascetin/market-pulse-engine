"""Serving layer: the Plotly Dash command centre.

Import ``app`` lazily (``from .dashboard.app import create_app``) so that
``python -m market_pulse_engine.dashboard.app`` runs without a double import.
"""

__all__ = ["app", "components", "figures", "queries", "theme"]
