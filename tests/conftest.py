"""Shared fixtures.

Every test runs against a throwaway DuckDB file, so the suite never touches the
developer's real database and tests cannot leak state into one another.
"""

from __future__ import annotations

import pytest

from market_pulse_engine.db import connection
from market_pulse_engine.db import init as db_init


@pytest.fixture
def db(tmp_path):
    """An initialised, empty database scoped to one test."""
    connection.use_database(tmp_path / "test.duckdb")
    db_init.apply_schema()
    yield connection.get_connection()
    connection.use_database(None)


@pytest.fixture
def run_id() -> str:
    return "run_test_000001"
