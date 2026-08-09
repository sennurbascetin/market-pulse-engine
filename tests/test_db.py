"""Database plumbing: lock reporting and index repair.

Both behaviours exist because of real failures seen in operation — a second
engine instance started against a running one, and an index left inconsistent
by a process killed mid-write.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from market_pulse_engine.db import connection, repair
from market_pulse_engine.db.connection import DatabaseLockedError

LOCK_ERROR = (
    'IO Error: Could not set lock on file "/tmp/market_pulse.duckdb": '
    "Conflicting lock is held in /usr/bin/python (PID 4242) by user someone."
)


# ---------------------------------------------------------------------------
# Lock reporting
# ---------------------------------------------------------------------------
def test_locked_message_names_the_offending_process():
    message = connection._locked_message(Path("/tmp/market_pulse.duckdb"), Exception(LOCK_ERROR))

    assert "/tmp/market_pulse.duckdb" in message
    assert "PID 4242" in message
    assert "kill 4242" in message          # an actionable next step
    assert "one read-write process" in message


def test_locked_message_without_a_pid_still_advises():
    message = connection._locked_message(Path("/tmp/x.duckdb"), Exception("could not set lock"))
    assert "Stop the other instance" in message
    assert "kill" not in message           # no PID to offer


def test_lock_failure_is_translated(tmp_path, monkeypatch):
    """A raw DuckDB IOException about locking becomes a readable error."""
    connection.close()
    connection.use_database(tmp_path / "locked.duckdb")

    def _raise(*_args, **_kwargs):
        raise duckdb.IOException(LOCK_ERROR)

    monkeypatch.setattr(duckdb, "connect", _raise)
    with pytest.raises(DatabaseLockedError) as caught:
        connection.get_connection()
    assert "PID 4242" in str(caught.value)

    connection.use_database(None)


def test_non_lock_io_errors_are_not_swallowed(tmp_path, monkeypatch):
    connection.close()
    connection.use_database(tmp_path / "broken.duckdb")

    def _raise(*_args, **_kwargs):
        raise duckdb.IOException("IO Error: disk is on fire")

    monkeypatch.setattr(duckdb, "connect", _raise)
    with pytest.raises(duckdb.IOException):
        connection.get_connection()

    connection.use_database(None)


# ---------------------------------------------------------------------------
# Index repair
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("FATAL Error: Failed to delete all rows from index. Only deleted 0 out of 109 rows.", True),
        ("Could not find node in index", True),
        ("Constraint Error: duplicate key", False),
        ("IO Error: could not set lock on file", False),
    ],
)
def test_index_failure_detection(message, expected):
    assert repair.is_index_failure(Exception(message)) is expected


def test_rebuild_table_preserves_every_row(db):
    """Recovery must never regenerate data — Bronze payloads are irreplaceable."""
    db.execute(
        """
        INSERT INTO bronze.raw_news (article_id, run_id, feed_name, feed_url, ingested_at, payload)
        SELECT 'id_' || i, 'r', 'Feed', 'https://x', now(), '{"t": 1}'::JSON
        FROM range(50) AS t(i)
        """
    )
    before = db.execute("SELECT count(*) FROM bronze.raw_news").fetchone()[0]
    checksum = db.execute("SELECT sum(hash(article_id)) FROM bronze.raw_news").fetchone()[0]
    assert before == 50

    restored = repair.rebuild_table("bronze.raw_news")

    conn = connection.get_connection()
    assert restored == before
    assert conn.execute("SELECT count(*) FROM bronze.raw_news").fetchone()[0] == before
    assert conn.execute("SELECT sum(hash(article_id)) FROM bronze.raw_news").fetchone()[0] == checksum
    # The primary key must be back in force after the rebuild.
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            """
            INSERT INTO bronze.raw_news (article_id, run_id, feed_name, feed_url, ingested_at, payload)
            VALUES ('id_0', 'r', 'Feed', 'https://x', now(), '{}'::JSON)
            """
        )
    # And no staging table is left behind.
    leftovers = conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE '__rebuild%'"
    ).fetchone()[0]
    assert leftovers == 0


def test_run_with_repair_passes_through_unrelated_errors(db):
    def boom():
        raise ValueError("nothing to do with indexes")

    with pytest.raises(ValueError):
        repair.run_with_repair("bronze.raw_news", boom)


def test_run_with_repair_retries_once_after_rebuilding(db):
    """The first call fails with an index abort; the retry must succeed."""
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError(
                "FATAL Error: Invalid Input Error: Failed to delete all rows from index. "
                "Only deleted 0 out of 3 rows."
            )
        return "recovered"

    assert repair.run_with_repair("bronze.raw_news", flaky) == "recovered"
    assert attempts["n"] == 2


def test_run_with_repair_reraises_a_persistent_failure(db):
    """A second identical failure is not an index problem — surface it."""
    def always_fails():
        raise RuntimeError("Failed to delete all rows from index. Only deleted 0 out of 3 rows.")

    with pytest.raises(RuntimeError):
        repair.run_with_repair("bronze.raw_news", always_fails)
