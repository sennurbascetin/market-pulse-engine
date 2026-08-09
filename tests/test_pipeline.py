"""Phase 6 — orchestration, market-hours gating and run bookkeeping."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_pulse_engine.config import CONFIG
from market_pulse_engine.ingestion import market_hours
from market_pulse_engine.pipeline.orchestrator import Orchestrator

ET = market_hours.MARKET_TIMEZONE


def _at(year, month, day, hour, minute=0):
    """A UTC instant expressed from a US-Eastern wall clock."""
    return datetime(year, month, day, hour, minute, tzinfo=ET).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (_at(2026, 8, 5, 10, 0), True),    # Wednesday mid-session
        (_at(2026, 8, 5, 9, 29), False),   # one minute before the open
        (_at(2026, 8, 5, 9, 30), True),    # the open itself
        (_at(2026, 8, 5, 16, 0), False),   # the close is exclusive
        (_at(2026, 8, 8, 11, 0), False),   # Saturday
        (_at(2026, 8, 9, 11, 0), False),   # Sunday
        (_at(2026, 7, 3, 11, 0), False),   # Independence Day (observed)
    ],
)
def test_is_market_open(moment, expected):
    assert market_hours.is_market_open(moment) is expected


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (_at(2026, 8, 5, 8, 0), "pre_market"),
        (_at(2026, 8, 5, 12, 0), "open"),
        (_at(2026, 8, 5, 18, 0), "after_hours"),
        (_at(2026, 8, 9, 12, 0), "closed"),
    ],
)
def test_session_state(moment, expected):
    assert market_hours.session_state(moment) == expected


def test_crypto_is_never_gated():
    sunday = _at(2026, 8, 9, 3, 0)
    assert market_hours.is_live_for("BTC-USD", sunday) is True
    assert market_hours.is_live_for("AAPL", sunday) is False


def test_next_market_open_skips_the_weekend():
    friday_evening = _at(2026, 8, 7, 18, 0)
    nxt = market_hours.next_market_open(friday_evening)
    assert nxt.weekday() == 0            # Monday
    assert (nxt.hour, nxt.minute) == (9, 30)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _stub_ingestion(monkeypatch, module):
    """Replace the three connectors with no-op successes."""
    from market_pulse_engine.ingestion.base import IngestResult

    monkeypatch.setattr(module, "fetch_news", lambda *_a, **_kw: IngestResult("rss_news", 3, 3))
    monkeypatch.setattr(module, "fetch_sentiment_index", lambda *_a, **_kw: IngestResult("cnn", 1, 1))
    monkeypatch.setattr(module, "fetch_quotes", lambda *_a, **_kw: IngestResult("yfinance", 2, 2))


def test_cycle_records_a_complete_run_log(db, monkeypatch):
    from market_pulse_engine.pipeline import orchestrator as module

    _stub_ingestion(monkeypatch, module)
    monkeypatch.setattr(module, "is_market_open", lambda *_a, **_kw: True)

    report = Orchestrator(provider=None).run_once()

    assert report.status == "success"
    assert report.mode == "live"
    assert report.records_ingested == 6

    row = db.execute(
        """
        SELECT status, mode, duration_ms, records_ingested, finished_at, error
        FROM pipeline.run_log WHERE run_id = ?
        """,
        [report.run_id],
    ).fetchone()
    assert row[0] == "success"
    assert row[1] == "live"
    assert row[2] >= 0
    assert row[3] == 6
    assert row[4] is not None      # finished_at closed out
    assert row[5] is None          # no error recorded


def test_outside_the_session_only_crypto_prices_are_requested(db, monkeypatch):
    """Polling a closed exchange just re-reads the same close."""
    from market_pulse_engine.ingestion.base import IngestResult
    from market_pulse_engine.pipeline import orchestrator as module

    requested: list[list[str]] = []

    def _capture(_run_id, symbols):
        requested.append(list(symbols))
        return IngestResult("yfinance", len(symbols), len(symbols))

    _stub_ingestion(monkeypatch, module)
    monkeypatch.setattr(module, "fetch_quotes", _capture)
    monkeypatch.setattr(module, "is_market_open", lambda *_a, **_kw: False)

    report = Orchestrator(provider=None).run_once()

    assert report.mode == "after_hours"
    assert requested, "the crypto leg should still run"
    assert all(CONFIG.is_crypto(ticker) for ticker in requested[0])


def test_a_failing_stage_degrades_the_run_without_raising(db, monkeypatch):
    from market_pulse_engine.pipeline import orchestrator as module

    _stub_ingestion(monkeypatch, module)
    monkeypatch.setattr(module, "is_market_open", lambda *_a, **_kw: True)

    def explode(*_args, **_kwargs):
        raise RuntimeError("feed down")

    monkeypatch.setattr(module, "fetch_news", explode)

    report = Orchestrator(provider=None).run_once()

    assert report.status in {"partial", "failed"}
    assert any("ingest.news" in message for message in report.errors)
    stored_error = db.execute(
        "SELECT error FROM pipeline.run_log WHERE run_id = ?", [report.run_id]
    ).fetchone()[0]
    assert stored_error and "feed down" in stored_error


def test_llm_stage_is_throttled(db):
    """The paid stage runs on the first cycle, then every Nth.

    Driven off the configured value rather than a patched one — ``Config`` is a
    frozen dataclass on purpose, so settings cannot drift at runtime.
    """
    every = max(1, CONFIG.llm.every_n_runs)
    orchestrator = Orchestrator(provider=None)

    fired = []
    for cycle in range(1, every * 2 + 2):
        orchestrator._cycle = cycle
        fired.append(orchestrator._should_run_llm())

    assert fired[0] is True                                  # always the first cycle
    assert fired[every - 1] is True                          # then every Nth
    assert fired[every * 2 - 1] is True
    assert sum(fired) == len([c for c in range(1, every * 2 + 2) if c == 1 or c % every == 0])
    if every > 1:
        assert fired[1] is False
