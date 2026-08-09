"""Every read the dashboard performs, in one place.

Keeping the SQL here rather than inside callbacks means the serving layer has a
single, reviewable data contract, and each panel's query can be run by hand in a
DuckDB shell when a number looks wrong.

All functions are defensive: an empty database returns empty structures rather
than raising, so the dashboard renders on the very first page load — before the
first pipeline cycle has finished.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..config import CONFIG
from ..db.connection import get_connection
from ..utils import ensure_utc, to_float


def _as_list(value: Any) -> list[str]:
    """Coerce a DuckDB ``VARCHAR[]`` cell to a plain list.

    Through ``fetchdf`` these arrive as NumPy arrays, whose truthiness raises,
    so the usual ``value or []`` idiom is not safe here.
    """
    if value is None:
        return []
    return [str(item) for item in value]


def _df(sql: str, params: list[Any] | None = None) -> pd.DataFrame:
    """Run a query, returning an empty frame if anything goes wrong."""
    try:
        return get_connection().execute(sql, params or []).fetchdf()
    except Exception:  # noqa: BLE001 - a mid-transform read must not break the UI
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Panel 1 — live ticker tape
# ---------------------------------------------------------------------------
def ticker_tape() -> list[dict[str, Any]]:
    """Latest price and intraday change for every watchlist ticker."""
    frame = _df(
        """
        SELECT ticker, price, intraday_return_pct, volume_z_score, is_live, observed_at
        FROM gold.quotes_enriched
        QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY observed_at DESC) = 1
        ORDER BY ticker
        """
    )
    if frame.empty:
        return []
    return [
        {
            "ticker": row.ticker,
            "price": to_float(row.price),
            "change_pct": to_float(row.intraday_return_pct),
            "volume_z": to_float(row.volume_z_score),
            "is_live": bool(row.is_live),
            "observed_at": ensure_utc(row.observed_at),
        }
        for row in frame.itertuples()
    ]


# ---------------------------------------------------------------------------
# Panel 2 — Market Pulse briefing
# ---------------------------------------------------------------------------
def latest_narrative() -> dict[str, Any] | None:
    frame = _df(
        """
        SELECT headline, narrative, regime, provider, model, generated_at
        FROM platinum.market_pulse_narratives
        ORDER BY generated_at DESC LIMIT 1
        """
    )
    if frame.empty:
        return None
    row = frame.iloc[0]
    return {
        "headline": row["headline"],
        "narrative": row["narrative"],
        "regime": row["regime"],
        "provider": row["provider"],
        "model": row["model"],
        "generated_at": ensure_utc(row["generated_at"]),
    }


# ---------------------------------------------------------------------------
# Panel 3 — Fear & Greed gauge
# ---------------------------------------------------------------------------
def fear_greed() -> dict[str, Any] | None:
    frame = _df(
        """
        SELECT score, label, previous_close_score, week_ago_score, month_ago_score, observed_at
        FROM silver.sentiment_index ORDER BY observed_at DESC LIMIT 1
        """
    )
    if frame.empty:
        return None
    row = frame.iloc[0]
    return {
        "score": to_float(row["score"]),
        "label": row["label"],
        "previous_close": to_float(row["previous_close_score"]),
        "week_ago": to_float(row["week_ago_score"]),
        "month_ago": to_float(row["month_ago_score"]),
        "observed_at": ensure_utc(row["observed_at"]),
    }


# ---------------------------------------------------------------------------
# Panel 4 — price chart with MA overlays and anomaly markers
# ---------------------------------------------------------------------------
def price_series(ticker: str, limit: int | None = None) -> pd.DataFrame:
    """Most recent observations for one ticker, oldest first."""
    frame = _df(
        """
        SELECT observed_at, price, open, day_high, day_low, volume_delta,
               ma_short, ma_mid, ma_long, vwap
        FROM gold.quotes_enriched
        WHERE ticker = ?
        ORDER BY observed_at DESC
        LIMIT ?
        """,
        [ticker, limit or CONFIG.dashboard.chart_points],
    )
    return frame.iloc[::-1].reset_index(drop=True) if not frame.empty else frame


def anomalies_for(ticker: str, since=None) -> pd.DataFrame:
    """Anomalies for one ticker, joined to the price they occurred at."""
    return _df(
        """
        SELECT a.observed_at, a.anomaly_type, a.severity, a.z_score, a.description,
               q.price
        FROM platinum.anomalies a
        JOIN gold.quotes_enriched q
          ON q.ticker = a.ticker AND q.observed_at = a.observed_at
        WHERE a.ticker = ?
          AND (? IS NULL OR a.observed_at >= ?)
        ORDER BY a.observed_at
        """,
        [ticker, since, since],
    )


# ---------------------------------------------------------------------------
# Panel 5 — volume heatmap
# ---------------------------------------------------------------------------
def volume_heatmap(days: int = 5) -> pd.DataFrame:
    """Hour-of-day trading activity per ticker, normalised to each ticker's peak.

    Two deliberate choices:

    * **Hour of day, not wall-clock hours.** A rolling 24-hour window shows only
      crypto over a weekend, because the equities genuinely did not trade.
      Folding several days onto a 0–23 axis instead surfaces each asset's
      *activity profile* — the US session block for equities, round-the-clock
      for crypto — which is what the panel is actually asked to show.
    * **Percentage of the ticker's own peak.** Raw volumes are not comparable
      across the watchlist: BTC-USD reports notional in the billions while AAPL
      reports share counts in the millions. Normalising per row makes the grid
      readable; the raw figure stays available on hover.

    The 0–23 grid is generated rather than derived, so a quiet hour renders as a
    dark cell instead of a hole.
    """
    return _df(
        f"""
        WITH hours AS (SELECT unnest(range(0, 24)) AS hour_of_day),
        symbols AS (SELECT DISTINCT ticker FROM gold.quotes_enriched),
        grid AS (SELECT ticker, hour_of_day FROM symbols CROSS JOIN hours),
        hourly AS (
            SELECT ticker,
                   CAST(date_part('hour', observed_at) AS INTEGER) AS hour_of_day,
                   sum(COALESCE(volume_delta, 0))                   AS volume
            FROM gold.quotes_enriched
            WHERE observed_at >= now() - INTERVAL {int(days)} DAY
            GROUP BY 1, 2
        ),
        filled AS (
            SELECT g.ticker, g.hour_of_day, COALESCE(h.volume, 0) AS volume
            FROM grid g
            LEFT JOIN hourly h USING (ticker, hour_of_day)
        )
        SELECT ticker, hour_of_day, volume,
               100.0 * volume / NULLIF(max(volume) OVER (PARTITION BY ticker), 0)
                   AS pct_of_peak
        FROM filled
        ORDER BY ticker, hour_of_day
        """
    )


# ---------------------------------------------------------------------------
# Panel 6 — sentiment feed
# ---------------------------------------------------------------------------
def sentiment_feed(limit: int = 25) -> list[dict[str, Any]]:
    frame = _df(
        """
        SELECT a.title, a.source, a.url, a.published_at,
               s.sentiment, s.confidence, s.key_themes, s.tickers_impacted, s.provider
        FROM platinum.news_sentiment s
        JOIN silver.news_articles a USING (article_id)
        ORDER BY COALESCE(a.published_at, s.scored_at) DESC
        LIMIT ?
        """,
        [limit],
    )
    if frame.empty:
        return []
    return [
        {
            "title": row.title,
            "source": row.source,
            "url": row.url,
            "published_at": ensure_utc(row.published_at),
            "sentiment": row.sentiment,
            "confidence": to_float(row.confidence),
            "themes": _as_list(row.key_themes),
            "tickers": _as_list(row.tickers_impacted),
            "provider": row.provider,
        }
        for row in frame.itertuples()
    ]


# ---------------------------------------------------------------------------
# Panel 7 — anomaly alert log
# ---------------------------------------------------------------------------
def anomaly_log(limit: int = 25) -> list[dict[str, Any]]:
    frame = _df(
        """
        SELECT ticker, observed_at, anomaly_type, severity, direction, z_score, description
        FROM platinum.anomalies
        ORDER BY observed_at DESC, abs(z_score) DESC
        LIMIT ?
        """,
        [limit],
    )
    if frame.empty:
        return []
    return [
        {
            "ticker": row.ticker,
            "observed_at": ensure_utc(row.observed_at),
            "anomaly_type": row.anomaly_type,
            "severity": row.severity,
            "direction": row.direction,
            "z_score": to_float(row.z_score),
            "description": row.description,
        }
        for row in frame.itertuples()
    ]


# ---------------------------------------------------------------------------
# Panel 8 — pipeline health
# ---------------------------------------------------------------------------
def pipeline_health() -> dict[str, Any]:
    """Last run, session totals and error count for the status bar."""
    last = _df(
        """
        SELECT run_id, started_at, finished_at, duration_ms, status, mode,
               records_ingested, anomalies_detected, llm_tokens_used, llm_provider, error
        FROM pipeline.run_log ORDER BY started_at DESC LIMIT 1
        """
    )
    totals = _df(
        """
        SELECT count(*)                             AS runs,
               COALESCE(sum(records_ingested), 0)   AS records,
               COALESCE(sum(anomalies_detected), 0) AS anomalies,
               COALESCE(sum(llm_tokens_used), 0)    AS tokens,
               COALESCE(sum(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failures,
               min(started_at)                      AS first_run
        FROM pipeline.run_log
        """
    )

    health: dict[str, Any] = {
        "last_run_at": None, "status": "idle", "mode": "-", "duration_ms": None,
        "records_ingested": 0, "anomalies_detected": 0, "llm_provider": None,
        "error": None, "runs": 0, "total_records": 0, "total_anomalies": 0,
        "total_tokens": 0, "failures": 0, "first_run": None,
    }

    if not last.empty:
        row = last.iloc[0]
        health |= {
            "last_run_at": ensure_utc(row["finished_at"] or row["started_at"]),
            "status": row["status"],
            "mode": row["mode"],
            "duration_ms": to_float(row["duration_ms"]),
            "records_ingested": int(row["records_ingested"] or 0),
            "anomalies_detected": int(row["anomalies_detected"] or 0),
            "llm_provider": row["llm_provider"],
            "error": row["error"],
        }
    if not totals.empty:
        row = totals.iloc[0]
        health |= {
            "runs": int(row["runs"] or 0),
            "total_records": int(row["records"] or 0),
            "total_anomalies": int(row["anomalies"] or 0),
            "total_tokens": int(row["tokens"] or 0),
            "failures": int(row["failures"] or 0),
            "first_run": ensure_utc(row["first_run"]),
        }
    return health


def layer_counts() -> dict[str, int]:
    """Row counts per medallion table, for the footer readout."""
    counts: dict[str, int] = {}
    for table in (
        "bronze.raw_quotes", "bronze.raw_news", "bronze.raw_sentiment_index",
        "silver.quotes", "silver.news_articles", "gold.quotes_enriched",
        "platinum.anomalies", "platinum.news_sentiment",
        "platinum.market_pulse_narratives",
    ):
        frame = _df(f"SELECT count(*) AS n FROM {table}")
        counts[table] = int(frame.iloc[0]["n"]) if not frame.empty else 0
    return counts


def available_tickers() -> list[str]:
    """Tickers that actually have Gold data, falling back to the watchlist."""
    frame = _df("SELECT DISTINCT ticker FROM gold.quotes_enriched ORDER BY ticker")
    if frame.empty:
        return list(CONFIG.watchlist)
    return frame["ticker"].tolist()
