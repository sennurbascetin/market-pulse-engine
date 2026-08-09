"""Synthetic-data builders shared by the test modules.

``tests/`` is not a package, so pytest puts this directory on ``sys.path`` and
these are imported as ``from helpers import ...``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from market_pulse_engine.utils import stable_id


def insert_raw_quote(
    conn,
    ticker: str,
    observed_at: datetime,
    *,
    price: float,
    volume: int = 1_000,
    open_price: float | None = None,
    source: str = "yfinance_history",
    run_id: str = "run_test_000001",
    **extra,
) -> None:
    """Land one synthetic payload in ``bronze.raw_quotes``."""
    payload = {
        "currentPrice": price,
        "open": open_price if open_price is not None else price,
        "dayHigh": max(price, open_price or price),
        "dayLow": min(price, open_price or price),
        "volume": volume,
        **extra,
    }
    conn.execute(
        """
        INSERT INTO bronze.raw_quotes
            (quote_id, run_id, ticker, source, observed_at, ingested_at, is_live, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?::JSON) ON CONFLICT DO NOTHING
        """,
        [
            stable_id(ticker, observed_at.isoformat(), source),
            run_id, ticker, source, observed_at, observed_at, False,
            json.dumps(payload),
        ],
    )


def seed_series(
    conn,
    ticker: str = "TEST",
    *,
    count: int = 80,
    start_price: float = 100.0,
    step: float = 0.0,
    volume: int = 1_000,
    volumes: list[int] | None = None,
    start: datetime | None = None,
    interval_minutes: int = 5,
) -> list[datetime]:
    """Land a deterministic price/volume series and return its timestamps.

    A flat series (``step=0``) is the useful baseline for anomaly tests: any
    injected spike is then unambiguously the only outlier.
    """
    start = start or datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    stamps: list[datetime] = []
    for index in range(count):
        stamp = start + timedelta(minutes=interval_minutes * index)
        insert_raw_quote(
            conn, ticker, stamp,
            price=start_price + step * index,
            volume=volumes[index] if volumes is not None else volume,
            open_price=start_price,
        )
        stamps.append(stamp)
    return stamps
