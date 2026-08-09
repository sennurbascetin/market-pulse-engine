"""Demo mode — keeps the board alive when the markets are shut.

Markets are closed most of the week, and a portfolio demo that shows a frozen
tape sells nothing. With ``MPE_DEMO_MODE=true`` the engine appends a synthetic
tick per ticker per cycle, continuing from that ticker's genuine last close with
a random walk calibrated to its own recently observed volatility.

**Synthetic rows are labelled, not disguised.** They land with
``source = 'demo_synthetic'`` and ``is_live = False``, the run log records
``mode = 'demo'``, and the dashboard status bar shows it. Nothing downstream has
to trust a flag it cannot see.
"""

from __future__ import annotations

import json
import random
from typing import Any

from ..config import CONFIG
from ..db.connection import get_connection
from ..ingestion.base import IngestResult, insert_bronze
from ..ingestion.price_fetcher import BRONZE_COLUMNS
from ..logging_setup import get_logger
from ..utils import stable_id, to_float, utcnow

log = get_logger("pipeline.demo")

#: Fallback per-tick volatility when a ticker has no measured history.
DEFAULT_SIGMA = 0.0012


def _seeds() -> list[dict[str, Any]]:
    """Latest real price, volatility and typical volume for each ticker."""
    rows = get_connection().execute(
        """
        SELECT ticker,
               arg_max(price, observed_at)      AS last_price,
               avg(NULLIF(volatility, 0))       AS sigma,
               median(NULLIF(volume_delta, 0))  AS typical_volume
        FROM gold.quotes_enriched
        GROUP BY ticker
        """
    ).fetchall()
    return [
        {
            "ticker": row[0],
            "last_price": to_float(row[1]),
            "sigma": to_float(row[2]) or DEFAULT_SIGMA,
            "typical_volume": to_float(row[3]) or 100_000.0,
        }
        for row in rows
        if to_float(row[1])
    ]


def synthesise(run_id: str, rng: random.Random | None = None) -> IngestResult:
    """Append one synthetic tick per ticker to Bronze."""
    random_source = rng or random.Random()
    result = IngestResult(source="demo_synthetic")
    now = utcnow()
    rows: list[list[Any]] = []

    for seed in _seeds():
        sigma = min(max(seed["sigma"], 0.0002), 0.02)
        drift = random_source.gauss(0.0, sigma)
        price = round(seed["last_price"] * (1 + drift), 6)
        # Volume is lognormal-ish around the ticker's own median interval volume.
        volume = max(0, int(seed["typical_volume"] * random_source.lognormvariate(0.0, 0.45)))

        payload = {
            "currentPrice": price,
            "open": seed["last_price"],
            "dayHigh": max(price, seed["last_price"]),
            "dayLow": min(price, seed["last_price"]),
            "volume": volume,
            "_synthetic": True,
            "_seeded_from": seed["last_price"],
            "_observed_at": now.isoformat(),
            "_is_live": False,
        }
        rows.append(
            [
                stable_id(seed["ticker"], now.isoformat(), "demo"),
                run_id,
                seed["ticker"],
                "demo_synthetic",
                now,
                now,
                False,
                json.dumps(payload, default=str),
            ]
        )

    result.fetched = len(rows)
    result.written = insert_bronze("bronze.raw_quotes", BRONZE_COLUMNS, rows)
    result.duplicates = result.fetched - result.written

    log.info("demo ticks generated", extra=result.as_log_fields() | {"run_id": run_id})
    return result


def is_enabled() -> bool:
    return CONFIG.demo_mode
