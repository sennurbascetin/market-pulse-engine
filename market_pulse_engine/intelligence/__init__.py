"""Platinum layer: statistical anomaly detection and LLM narrative analysis."""

from .anomaly_detector import Anomaly, detect, find_anomalies
from .llm_analyst import AnalystResult, build_context, latest_narrative
from .llm_provider import get_provider

__all__ = [
    "AnalystResult",
    "Anomaly",
    "build_context",
    "detect",
    "find_anomalies",
    "get_provider",
    "latest_narrative",
]
