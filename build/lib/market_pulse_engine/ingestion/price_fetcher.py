"""Sub-module 2A — price ingestion via yfinance.

Two entry points, both landing in ``bronze.raw_quotes``:

:func:`fetch_quotes`
    One live snapshot per watchlist ticker. During the regular session the
    record is flagged ``is_live = True``; outside it the last known close is
    captured and flagged ``is_live = False``.

:func:`backfill_history`
    Real intraday OHLCV bars pulled once at start-up. Without this the Gold
    layer would need hours of uptime before its rolling windows (moving
    averages, z-scores, VWAP) became meaningful — the backfill gives the
    engine a genuine market history to reason over from the first run.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from ..config import CONFIG
from ..logging_setup import get_logger
from ..utils import ensure_utc, epoch_to_utc, stable_id, to_float, to_int, utcnow
from .base import IngestResult, insert_bronze
from .market_hours import is_live_for

log = get_logger("ingestion.price")

BRONZE_COLUMNS = (
    "quote_id", "run_id", "ticker", "source",
    "observed_at", "ingested_at", "is_live", "payload",
)

#: Fields lifted out of the yfinance payload into Silver. The raw payload is
#: still stored verbatim, so adding a field later needs no re-ingestion.
QUOTE_FIELDS = (
    "currentPrice", "regularMarketPrice", "open", "dayHigh", "dayLow",
    "previousClose", "volume", "regularMarketVolume", "marketCap",
    "fiftyDayAverage", "twoHundredDayAverage", "currency", "exchange",
    "marketState", "regularMarketTime", "shortName",
)


def _snapshot(ticker: str) -> dict[str, Any] | None:
    """Pull one quote payload, preferring ``.info`` and degrading to ``fast_info``."""
    import yfinance as yf  # imported lazily: keeps unit tests import-light

    handle = yf.Ticker(ticker)
    payload: dict[str, Any] = {}

    try:
        info = handle.info or {}
        payload = {key: info.get(key) for key in QUOTE_FIELDS if info.get(key) is not None}
    except Exception as exc:  # noqa: BLE001 - yfinance raises a wide variety
        log.warning("info lookup failed, falling back to fast_info",
                    extra={"ticker": ticker, "error": str(exc)})

    if not payload.get("currentPrice") and not payload.get("regularMarketPrice"):
        try:
            fast = handle.fast_info
            payload.update(
                {
                    "currentPrice": fast.get("lastPrice"),
                    "open": fast.get("open"),
                    "dayHigh": fast.get("dayHigh"),
                    "dayLow": fast.get("dayLow"),
                    "previousClose": fast.get("previousClose"),
                    "volume": fast.get("lastVolume"),
                    "marketCap": fast.get("marketCap"),
                    "fiftyDayAverage": fast.get("fiftyDayAverage"),
                    "twoHundredDayAverage": fast.get("twoHundredDayAverage"),
                    "currency": fast.get("currency"),
                    "exchange": fast.get("exchange"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.error("quote unavailable", extra={"ticker": ticker, "error": str(exc)})
            return None

    price = to_float(payload.get("currentPrice")) or to_float(payload.get("regularMarketPrice"))
    if price is None:
        return None
    payload["currentPrice"] = price
    return payload


def _quote_row(ticker: str, payload: dict[str, Any], run_id: str) -> list[Any] | None:
    """Shape one snapshot payload into a ``bronze.raw_quotes`` row."""
    now = utcnow()
    live = is_live_for(ticker, now)

    # Prefer the exchange's own timestamp; it is what makes an observation
    # unique. Falling back to ingestion time keeps unusual payloads usable.
    observed_at = epoch_to_utc(payload.get("regularMarketTime")) or now
    if live:
        # During the session yfinance repeats the same regularMarketTime for up
        # to a minute; the poll instant is the higher-resolution truth.
        observed_at = now

    payload = dict(payload)
    payload["_is_live"] = live
    payload["_observed_at"] = observed_at.isoformat()

    return [
        stable_id(ticker, observed_at.isoformat(), "snapshot"),
        run_id,
        ticker,
        "yfinance",
        observed_at,
        now,
        live,
        json.dumps(payload, default=str),
    ]


def fetch_quotes(run_id: str, tickers: list[str] | None = None) -> IngestResult:
    """Fetch a live snapshot for every watchlist ticker and land it in Bronze."""
    symbols = tickers or CONFIG.watchlist
    result = IngestResult(source="yfinance_quotes")
    rows: list[list[Any]] = []

    # yfinance calls are network-bound; fan out so the whole watchlist costs
    # roughly one round-trip rather than N.
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        payloads = list(pool.map(_snapshot, symbols))

    for ticker, payload in zip(symbols, payloads):
        if payload is None:
            result.errors.append(f"{ticker}: no quote returned")
            continue
        row = _quote_row(ticker, payload, run_id)
        if row is None:
            result.errors.append(f"{ticker}: payload missing a usable price")
            continue
        rows.append(row)

    result.fetched = len(rows)
    result.written = insert_bronze("bronze.raw_quotes", BRONZE_COLUMNS, rows)
    result.duplicates = result.fetched - result.written

    log.info("prices ingested", extra=result.as_log_fields() | {"run_id": run_id})
    return result


def backfill_history(
    run_id: str,
    tickers: list[str] | None = None,
    *,
    period: str = "5d",
    interval: str = "5m",
) -> IngestResult:
    """Land real intraday OHLCV bars in Bronze so Gold has history to work on.

    Bars are marked ``is_live = False`` and ``source = 'yfinance_history'``, so
    they are always distinguishable from live snapshots downstream.
    """
    import yfinance as yf

    symbols = tickers or CONFIG.watchlist
    result = IngestResult(source="yfinance_history")
    rows: list[list[Any]] = []
    now = utcnow()

    def _history(ticker: str) -> tuple[str, Any]:
        try:
            return ticker, yf.Ticker(ticker).history(period=period, interval=interval)
        except Exception as exc:  # noqa: BLE001
            log.warning("history unavailable", extra={"ticker": ticker, "error": str(exc)})
            return ticker, None

    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        frames = list(pool.map(_history, symbols))

    for ticker, frame in frames:
        if frame is None or frame.empty:
            result.errors.append(f"{ticker}: no history returned")
            continue

        # yfinance indexes equities in exchange-local time and crypto in UTC.
        bars = frame.tz_convert("UTC") if frame.index.tz is not None else frame.tz_localize("UTC")
        for stamp, bar in bars.iterrows():
            observed_at = ensure_utc(stamp.to_pydatetime() if hasattr(stamp, "to_pydatetime") else stamp)
            close = to_float(bar.get("Close"))
            if observed_at is None or close is None:
                continue
            payload = {
                "currentPrice": close,
                "open": to_float(bar.get("Open")),
                "dayHigh": to_float(bar.get("High")),
                "dayLow": to_float(bar.get("Low")),
                "volume": to_int(bar.get("Volume")),
                "interval": interval,
                "_source": "history",
                "_observed_at": observed_at.isoformat(),
                "_is_live": False,
            }
            rows.append(
                [
                    stable_id(ticker, observed_at.isoformat(), "history"),
                    run_id,
                    ticker,
                    "yfinance_history",
                    observed_at,
                    now,
                    False,
                    json.dumps(payload, default=str),
                ]
            )

    result.fetched = len(rows)
    result.written = insert_bronze("bronze.raw_quotes", BRONZE_COLUMNS, rows)
    result.duplicates = result.fetched - result.written

    log.info(
        "history backfilled",
        extra=result.as_log_fields() | {"run_id": run_id, "period": period, "interval": interval},
    )
    return result


def latest_observation(ticker: str) -> datetime | None:
    """Most recent Bronze observation timestamp for ``ticker`` (or ``None``)."""
    from ..db.connection import get_connection

    row = get_connection().execute(
        "SELECT max(observed_at) FROM bronze.raw_quotes WHERE ticker = ?", [ticker]
    ).fetchone()
    return ensure_utc(row[0]) if row and row[0] else None
