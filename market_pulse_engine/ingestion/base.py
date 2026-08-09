"""Shared plumbing for the Bronze connectors.

Every connector returns an :class:`IngestResult` so the orchestrator can log a
uniform record regardless of which source produced it, and every connector
writes through :func:`insert_bronze`, which makes ingestion idempotent: rows
carry a deterministic natural key and duplicates are silently discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..db.connection import transaction


@dataclass
class IngestResult:
    """Outcome of one connector invocation."""

    source: str
    fetched: int = 0
    written: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "fetched": self.fetched,
            "written": self.written,
            "duplicates": self.duplicates,
            "errors": len(self.errors),
        }

    def __str__(self) -> str:  # pragma: no cover - display only
        state = "ok" if self.ok else f"{len(self.errors)} error(s)"
        return (
            f"{self.source}: fetched={self.fetched} written={self.written} "
            f"duplicates={self.duplicates} [{state}]"
        )


def insert_bronze(
    table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    json_column: str = "payload",
) -> int:
    """Append ``rows`` to a Bronze table, skipping primary-key collisions.

    Returns the number of rows actually persisted.
    """
    if not rows:
        return 0

    placeholders = ", ".join(
        "?::JSON" if column == json_column else "?" for column in columns
    )
    statement = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    )

    with transaction() as conn:
        before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        conn.executemany(statement, [list(row) for row in rows])
        after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return after - before
