"""Create (or verify) the four-layer DuckDB schema.

Run directly::

    python -m market_pulse_engine.db.init

The operation is idempotent — running it against a populated database verifies
the structure without touching a single row.
"""

from __future__ import annotations

from pathlib import Path

from ..config import CONFIG
from ..logging_setup import get_logger
from .connection import get_connection, write_lock

log = get_logger("db.init")

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

LAYERS = ("bronze", "silver", "gold", "platinum", "pipeline")


def apply_schema() -> None:
    """Execute ``schema.sql`` against the configured database."""
    conn = get_connection()
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    with write_lock:
        # Every statement is IF NOT EXISTS, so a single batched execute is safe
        # and keeps the DDL file the one source of truth for the data model.
        conn.execute(ddl)


def describe() -> dict[str, list[str]]:
    """Return ``{schema: [table, ...]}`` for the medallion layers."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('bronze', 'silver', 'gold', 'platinum', 'pipeline')
        ORDER BY table_schema, table_name
        """
    ).fetchall()
    layout: dict[str, list[str]] = {layer: [] for layer in LAYERS}
    for schema_name, table_name in rows:
        layout.setdefault(schema_name, []).append(table_name)
    return layout


def row_counts() -> dict[str, int]:
    """Return ``{"schema.table": row_count}`` across every layer."""
    conn = get_connection()
    counts: dict[str, int] = {}
    for schema_name, tables in describe().items():
        for table in tables:
            fqn = f"{schema_name}.{table}"
            counts[fqn] = conn.execute(f"SELECT count(*) FROM {fqn}").fetchone()[0]
    return counts


def main() -> None:
    CONFIG.ensure_directories()
    apply_schema()
    layout = describe()
    total_tables = sum(len(tables) for tables in layout.values())

    log.info(
        "schema ready",
        extra={"db_path": str(CONFIG.db_path), "tables": total_tables},
    )
    print(f"\n  Market Pulse Engine — database ready at {CONFIG.db_path}\n")
    for layer in LAYERS:
        tables = layout.get(layer, [])
        print(f"  {layer:<9} {', '.join(tables) if tables else '(empty)'}")
    print(f"\n  {total_tables} tables across {len(LAYERS)} layers.")
    print(f"  Watchlist: {', '.join(CONFIG.watchlist)}\n")


if __name__ == "__main__":
    main()
