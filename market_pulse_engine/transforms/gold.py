"""Silver -> Gold: rolling metrics and session aggregates.

Everything here is DuckDB SQL. The whole enrichment — moving averages,
intraday and period returns, log-return volatility, VWAP, and three families of
z-score — is a single chained query over window functions, which runs in
milliseconds on an in-process OLAP engine and avoids row-by-row Python entirely.

Unlike the immutable Silver tables, Gold rows genuinely change as rolling
windows extend, so these transforms must overwrite. That takes DuckDB's index
delete path, so both are wrapped in the index-repair guard.
"""

from __future__ import annotations

from ..db.connection import transaction
from ..db.repair import run_with_repair
from ..logging_setup import get_logger
from .loader import load_sql, window_parameters
from .silver import TransformResult

log = get_logger("transforms.gold")


def _execute(name: str, table: str) -> TransformResult:
    with transaction() as conn:
        before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        conn.execute(load_sql(name))
        after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return TransformResult(table, before, after)


def build_quotes_enriched() -> TransformResult:
    """Compute per-observation analytics into ``gold.quotes_enriched``."""
    table = "gold.quotes_enriched"
    result = run_with_repair(table, lambda: _execute("gold_quotes_enriched", table))
    log.info(
        "gold enrichment",
        extra={"rows": result.rows_after, "added": result.rows_added} | window_parameters(),
    )
    return result


def build_daily_summary() -> TransformResult:
    """Roll enriched quotes up to one row per ticker per session."""
    table = "gold.daily_summary"
    result = run_with_repair(table, lambda: _execute("gold_daily_summary", table))
    log.info("gold daily summary", extra={"rows": result.rows_after, "added": result.rows_added})
    return result


def run_all() -> list[TransformResult]:
    """Run every Silver -> Gold transform, in dependency order."""
    return [build_quotes_enriched(), build_daily_summary()]
