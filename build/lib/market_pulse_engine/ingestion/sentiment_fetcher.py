"""Sub-module 2C — CNN Fear & Greed Index ingestion.

The endpoint is free and key-less but unofficial, so every failure mode is
absorbed: a bad status, a shape change, or a timeout logs a warning and returns
an empty result. The dashboard renders "Unavailable" rather than breaking.

The full ``graphdata`` document carries a year of daily history on every call.
Only the current reading and the latest value of each of the seven component
indicators are persisted, which keeps Bronze compact without losing anything
the intelligence layer or dashboard uses.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from ..config import CNN_FEAR_GREED_URL, CONFIG, HTTP_HEADERS
from ..logging_setup import get_logger
from ..utils import ensure_utc, stable_id, to_float, utcnow
from .base import IngestResult, insert_bronze

log = get_logger("ingestion.sentiment")

BRONZE_COLUMNS = ("reading_id", "run_id", "source", "observed_at", "ingested_at", "payload")

#: The seven sub-indicators CNN composites into the headline score.
COMPONENTS = (
    "market_momentum_sp500",
    "stock_price_strength",
    "stock_price_breadth",
    "put_call_options",
    "market_volatility_vix",
    "junk_bond_demand",
    "safe_haven_demand",
)

#: Score bands used when CNN omits its own rating string.
RATING_BANDS = (
    (25.0, "Extreme Fear"),
    (45.0, "Fear"),
    (55.0, "Neutral"),
    (75.0, "Greed"),
    (100.1, "Extreme Greed"),
)


def label_for(score: float) -> str:
    """Map a 0–100 composite score onto CNN's rating vocabulary."""
    for ceiling, label in RATING_BANDS:
        if score < ceiling:
            return label
    return "Extreme Greed"


def _normalise_rating(rating: Any, score: float) -> str:
    """Title-case CNN's rating (``"extreme fear"``) or derive one from the score."""
    if isinstance(rating, str) and rating.strip():
        return rating.strip().title()
    return label_for(score)


def fetch_sentiment_index(run_id: str, url: str = CNN_FEAR_GREED_URL) -> IngestResult:
    """Fetch the current Fear & Greed reading and land it in Bronze."""
    result = IngestResult(source="cnn_fear_greed")

    try:
        response = requests.get(url, headers=HTTP_HEADERS, timeout=CONFIG.request_timeout)
        response.raise_for_status()
        document = response.json()
    except Exception as exc:  # noqa: BLE001 - unofficial endpoint, treat all as soft failure
        result.errors.append(f"fear_greed: {exc}")
        log.warning("fear & greed unavailable", extra={"error": str(exc), "run_id": run_id})
        return result

    current = document.get("fear_and_greed") or {}
    score = to_float(current.get("score"))
    if score is None:
        result.errors.append("fear_greed: response contained no score")
        log.warning("fear & greed payload had no score", extra={"run_id": run_id})
        return result

    observed_at = ensure_utc(_parse_timestamp(current.get("timestamp"))) or utcnow()
    now = utcnow()

    payload = {
        "score": score,
        "rating": _normalise_rating(current.get("rating"), score),
        "timestamp": current.get("timestamp"),
        "previous_close": to_float(current.get("previous_close")),
        "previous_1_week": to_float(current.get("previous_1_week")),
        "previous_1_month": to_float(current.get("previous_1_month")),
        "previous_1_year": to_float(current.get("previous_1_year")),
        "components": {
            name: {
                "score": to_float((document.get(name) or {}).get("score")),
                "rating": (document.get(name) or {}).get("rating"),
            }
            for name in COMPONENTS
            if document.get(name)
        },
    }

    rows = [
        [
            stable_id("cnn_fear_greed", observed_at.isoformat()),
            run_id,
            "cnn_fear_greed",
            observed_at,
            now,
            json.dumps(payload, default=str),
        ]
    ]

    result.fetched = 1
    result.written = insert_bronze("bronze.raw_sentiment_index", BRONZE_COLUMNS, rows)
    result.duplicates = result.fetched - result.written

    log.info(
        "fear & greed ingested",
        extra=result.as_log_fields() | {"run_id": run_id, "score": score, "rating": payload["rating"]},
    )
    return result


def _parse_timestamp(value: Any):
    """CNN sends either an ISO-8601 string or epoch milliseconds."""
    from datetime import datetime, timezone

    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    number = to_float(value)
    if number is None:
        return None
    # Values above ~1e11 are milliseconds rather than seconds.
    seconds = number / 1000 if number > 1e11 else number
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
