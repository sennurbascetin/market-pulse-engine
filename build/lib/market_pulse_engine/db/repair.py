"""Recovery from a damaged DuckDB ART index.

DuckDB maintains an ART index behind every ``PRIMARY KEY``. If the process is
killed mid-write — ``kill``, a container stop, or an unlucky Ctrl-C — that index
can be left inconsistent with the table's rows. The next statement that has to
*delete* an index entry then aborts the whole database instance with:

    FATAL Error: Invalid Input Error: Failed to delete all rows from index.
    Only deleted 0 out of N rows.

Both ``INSERT OR REPLACE`` and ``ON CONFLICT DO UPDATE`` take that path;
``ON CONFLICT DO NOTHING`` does not, which is why the immutable Silver tables use
it. Where a genuine refresh is required the table stays exposed, so this module
detects the failure and heals it.

Recovery preserves every row: the data is copied aside, the table is dropped and
recreated from ``schema.sql`` (rebuilding the index from scratch), and the rows
are put back. Nothing is regenerated or lost — which matters because Bronze
payloads and LLM-authored Platinum records cannot be recomputed.
"""

from __future__ import annotations

from ..logging_setup import get_logger
from . import connection

log = get_logger("db.repair")

#: Substrings identifying the corrupt-index abort. DuckDB raises this as a
#: FatalException, which invalidates the whole database instance for the
#: process, so recovery must reconnect before it can do anything.
_INDEX_FAILURE_MARKERS = (
    "failed to delete all rows from index",
    "could not find node in index",
)


def is_index_failure(error: BaseException) -> bool:
    """True when ``error`` is the damaged-index abort described above."""
    message = str(error).lower()
    return any(marker in message for marker in _INDEX_FAILURE_MARKERS)


def rebuild_table(fqn: str) -> int:
    """Rebuild ``schema.table``'s index in place, preserving all rows.

    Returns the number of rows carried across. Safe to call on any table in the
    warehouse; it never regenerates data from upstream.
    """
    schema_name, _, table_name = fqn.partition(".")
    staging = f"{schema_name}.__rebuild_{table_name}"

    # The fatal error killed the database instance for this process; reconnect
    # before touching anything.
    connection.close()
    conn = connection.get_connection()

    log.warning("rebuilding damaged index", extra={"table": fqn})

    conn.execute(f"CREATE OR REPLACE TABLE {staging} AS SELECT * FROM {fqn}")
    preserved = conn.execute(f"SELECT count(*) FROM {staging}").fetchone()[0]

    conn.execute(f"DROP TABLE {fqn}")

    # schema.sql is the single source of truth for the definition, so replaying
    # it restores the exact columns, keys and indexes.
    from .init import apply_schema

    apply_schema()

    conn.execute(f"INSERT INTO {fqn} SELECT * FROM {staging}")
    restored = conn.execute(f"SELECT count(*) FROM {fqn}").fetchone()[0]
    conn.execute(f"DROP TABLE {staging}")

    if restored != preserved:  # pragma: no cover - defensive
        log.error(
            "row count changed during rebuild",
            extra={"table": fqn, "before": preserved, "after": restored},
        )
    log.info("index rebuilt", extra={"table": fqn, "rows": restored})
    return restored


def run_with_repair(table: str, action):
    """Run ``action``; on a damaged-index abort, rebuild ``table`` and retry once.

    A second failure is re-raised — at that point the problem is not a stale
    index and silently retrying would only hide it.
    """
    try:
        return action()
    except Exception as error:  # noqa: BLE001 - the abort type varies by version
        if not is_index_failure(error):
            raise
        log.warning(
            "index failure detected, attempting recovery",
            extra={"table": table, "error": str(error).splitlines()[0][:160]},
        )
        rebuild_table(table)
        return action()
