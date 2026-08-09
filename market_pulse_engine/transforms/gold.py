"""Silver -> Gold: rolling metrics and session aggregates.

Everything here is DuckDB SQL. The whole enrichment — moving averages,
intraday and period returns, log-return volatility, VWAP, and three families of
z-score — is a single chained query over window functions, which runs in
milliseconds on an in-process OLAP engine and avoids row-by-row Python entirely.
"""

from __future__ import annotations

from ..db.connection import transaction
from ..logging_setup import get_logger
from .loader import load_sql, window_parameters
from .silver import TransformResult

log = get_logger("transforms.gold")


def build_quotes_enriched() -> TransformResult:
    """Compute per-observation analytics into ``gold.quotes_enriched``."""
    with transaction() as conn:
        before = conn.execute("SELECT count(*) FROM gold.quotes_enriched").fetchone()[0]
        conn.execute(load_sql("gold_quotes_enriched"))
        after = conn.execute("SELECT count(*) FROM gold.quotes_enriched").fetchone()[0]

    result = TransformResult("gold.quotes_enriched", before, after)
    log.info("gold enrichment", extra={"rows": after, "added": result.rows_added} | window_parameters())
    return result


def build_daily_summary() -> TransformResult:
    """Roll enriched quotes up to one row per ticker per session."""
    with transaction() as conn:
        before = conn.execute("SELECT count(*) FROM gold.daily_summary").fetchone()[0]
        conn.execute(load_sql("gold_daily_summary"))
        after = conn.execute("SELECT count(*) FROM gold.daily_summary").fetchone()[0]

    result = TransformResult("gold.daily_summary", before, after)
    log.info("gold daily summary", extra={"rows": after, "added": result.rows_added})
    return result


def run_all() -> list[TransformResult]:
    """Run every Silver -> Gold transform, in dependency order."""
    return [build_quotes_enriched(), build_daily_summary()]
