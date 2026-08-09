"""Shared DuckDB access.

DuckDB is an *embedded* database: a file may be held read-write by exactly one
process. The Market Pulse Engine therefore runs the scheduler and the dashboard
inside a single process and shares one database instance between them.

Concurrency model
-----------------
* One root connection is opened lazily per process.
* Each thread gets its own cursor (``conn.cursor()``), which is an independent
  connection onto the same instance — this is DuckDB's supported way of doing
  multi-threaded access.
* Writers take :data:`write_lock` so two threads never open conflicting
  transactions on the same table.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb

from ..config import CONFIG

_root_lock = threading.Lock()
_root_connection: duckdb.DuckDBPyConnection | None = None
_thread_local = threading.local()
_path_override: Path | None = None

#: Serialises write transactions across the scheduler and dashboard threads.
write_lock = threading.RLock()


def use_database(path: Path | str | None) -> None:
    """Point the process at a different database file.

    Closes any open connection first. Passing ``None`` restores the configured
    path. This is the seam the test suite uses to run against a throwaway
    database instead of the developer's real one.
    """
    global _path_override
    close()
    _path_override = Path(path) if path is not None else None


def database_path() -> Path:
    """The database file currently in use."""
    return _path_override or CONFIG.db_path


def _root() -> duckdb.DuckDBPyConnection:
    """Open (once) and return the process-wide root connection."""
    global _root_connection
    if _root_connection is None:
        with _root_lock:
            if _root_connection is None:
                path = database_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                _root_connection = duckdb.connect(str(path))
                _configure(_root_connection)
    return _root_connection


def _configure(conn: duckdb.DuckDBPyConnection) -> None:
    """Pin session settings so behaviour never depends on the host machine.

    Without this, DuckDB renders ``TIMESTAMPTZ`` in the operating system's local
    zone, which would make the same query return different-looking timestamps on
    a developer laptop and in the container.
    """
    conn.execute("SET TimeZone = 'UTC'")


def get_connection() -> duckdb.DuckDBPyConnection:
    """Return this thread's cursor onto the shared database."""
    cursor = getattr(_thread_local, "cursor", None)
    if cursor is None:
        cursor = _root().cursor()
        _configure(cursor)
        _thread_local.cursor = cursor
    return cursor


@contextmanager
def transaction() -> Iterator[duckdb.DuckDBPyConnection]:
    """Run a block inside a write transaction, serialised process-wide.

    Commits on success, rolls back on any exception.
    """
    conn = get_connection()
    with write_lock:
        conn.execute("BEGIN TRANSACTION")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


def open_readonly(db_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open a separate read-only connection.

    Only usable when no other process holds the file read-write; the dashboard
    uses :func:`get_connection` instead when it runs alongside the scheduler.
    """
    return duckdb.connect(str(db_path or database_path()), read_only=True)


def close() -> None:
    """Close the root connection and forget per-thread cursors (used by tests)."""
    global _root_connection
    with _root_lock:
        if _root_connection is not None:
            _root_connection.close()
            _root_connection = None
    _thread_local.__dict__.pop("cursor", None)
