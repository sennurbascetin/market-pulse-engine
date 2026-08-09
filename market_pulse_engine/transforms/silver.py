"""Bronze -> Silver: clean, type, deduplicate.

Each transform is a single ``INSERT OR REPLACE ... SELECT`` executed inside one
transaction, so Silver is always consistent with the Bronze snapshot it was
derived from, and re-running the pipeline is idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db.connection import transaction
from ..logging_setup import get_logger
from .loader import load_sql

log = get_logger("transforms.silver")


@dataclass
class TransformResult:
    """Row counts before/after one transform step."""

    table: str
    rows_before: int
    rows_after: int

    @property
    def rows_added(self) -> int:
        return self.rows_after - self.rows_before

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.table}: {self.rows_after} rows (+{self.rows_added})"


def _run(name: str, table: str) -> TransformResult:
    """Execute one transform file and report the row delta."""
    with transaction() as conn:
        before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        conn.execute(load_sql(name))
        after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    result = TransformResult(table=table, rows_before=before, rows_after=after)
    log.info("silver transform", extra={"table": table, "rows": after, "added": result.rows_added})
    return result


def build_quotes() -> TransformResult:
    """Parse, type and deduplicate raw quotes into ``silver.quotes``."""
    return _run("silver_quotes", "silver.quotes")


def build_news() -> TransformResult:
    """Type raw RSS payloads into ``silver.news_articles``."""
    return _run("silver_news", "silver.news_articles")


def build_sentiment() -> TransformResult:
    """Type raw Fear & Greed readings into ``silver.sentiment_index``."""
    return _run("silver_sentiment", "silver.sentiment_index")


def run_all() -> list[TransformResult]:
    """Run every Bronze -> Silver transform."""
    return [build_quotes(), build_news(), build_sentiment()]
