"""Phase 3 — Silver and Gold transforms fed synthetic data.

Expected values are computed by hand in the assertions, so a regression in the
SQL shows up as a specific wrong number rather than "something changed".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from market_pulse_engine.transforms import gold, silver
from market_pulse_engine.utils import stable_id

from helpers import insert_raw_quote, seed_series

START = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)


def _build(conn):
    silver.run_all()
    gold.run_all()


def _gold_rows(conn, ticker="TEST"):
    return conn.execute(
        """
        SELECT observed_at, price, session_open, prev_price, ma_short, ma_mid, ma_long,
               intraday_return_pct, period_return_pct, log_return,
               volume_delta, volume_z_score, vwap, observation_index
        FROM gold.quotes_enriched WHERE ticker = ? ORDER BY observed_at
        """,
        [ticker],
    ).fetchall()


# ---------------------------------------------------------------------------
# Silver
# ---------------------------------------------------------------------------
def test_silver_types_and_row_count(db):
    seed_series(db, count=10)
    silver.run_all()

    rows = db.execute("SELECT count(*) FROM silver.quotes").fetchone()[0]
    assert rows == 10

    ticker, observed_at, price, volume_basis, source = db.execute(
        "SELECT ticker, observed_at, price, volume_basis, source FROM silver.quotes LIMIT 1"
    ).fetchone()
    assert ticker == "TEST"
    assert observed_at.tzinfo is not None
    assert isinstance(price, float)
    assert volume_basis == "bar"          # historical bars carry per-bar volume
    assert source == "yfinance_history"


def test_silver_deduplicates_preferring_the_live_snapshot(db):
    """Two payloads for the same instant collapse to one row; the snapshot wins."""
    stamp = START
    insert_raw_quote(db, "TEST", stamp, price=100.0, source="yfinance_history")
    # Same (ticker, observed_at) but a different source => a different Bronze id.
    insert_raw_quote(db, "TEST", stamp, price=101.0, source="yfinance")

    assert db.execute("SELECT count(*) FROM bronze.raw_quotes").fetchone()[0] == 2

    silver.run_all()
    rows = db.execute("SELECT price, volume_basis FROM silver.quotes").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(101.0)
    assert rows[0][1] == "cumulative"


def test_silver_fills_missing_fields(db):
    """A payload with only a price must still produce a complete Silver row."""
    db.execute(
        """
        INSERT INTO bronze.raw_quotes
            (quote_id, run_id, ticker, source, observed_at, ingested_at, is_live, payload)
        VALUES (?, 'r', 'TEST', 'yfinance', ?, ?, false, ?::JSON)
        """,
        [stable_id("sparse"), START, START, json.dumps({"currentPrice": 50.0})],
    )
    silver.run_all()

    open_price, high, low, volume, currency = db.execute(
        "SELECT open, day_high, day_low, volume, currency FROM silver.quotes"
    ).fetchone()
    assert open_price == pytest.approx(50.0)   # falls back to price
    assert high == pytest.approx(50.0)
    assert low == pytest.approx(50.0)
    assert volume == 0                          # null volume fills to zero
    assert currency == "USD"


def test_silver_rejects_rows_without_a_usable_price(db):
    db.execute(
        """
        INSERT INTO bronze.raw_quotes
            (quote_id, run_id, ticker, source, observed_at, ingested_at, is_live, payload)
        VALUES (?, 'r', 'TEST', 'yfinance', ?, ?, false, ?::JSON)
        """,
        [stable_id("bad"), START, START, json.dumps({"currentPrice": None})],
    )
    silver.run_all()
    assert db.execute("SELECT count(*) FROM silver.quotes").fetchone()[0] == 0


def test_silver_news_casts_ticker_array(db):
    payload = {
        "title": "Nvidia rallies", "summary": "", "url": "https://example.com/a",
        "source": "Test", "published_at": "2026-08-08T12:00:00+00:00",
        "tickers_mentioned": ["NVDA", "AAPL"],
    }
    db.execute(
        """
        INSERT INTO bronze.raw_news (article_id, run_id, feed_name, feed_url, ingested_at, payload)
        VALUES ('a1', 'r', 'Test', 'https://x', ?, ?::JSON)
        """,
        [START, json.dumps(payload)],
    )
    silver.run_all()

    title, summary, tickers = db.execute(
        "SELECT title, summary, tickers_mentioned FROM silver.news_articles"
    ).fetchone()
    assert title == "Nvidia rallies"
    assert summary == "Nvidia rallies"          # empty summary falls back to title
    assert list(tickers) == ["NVDA", "AAPL"]


# ---------------------------------------------------------------------------
# Gold — metric correctness
# ---------------------------------------------------------------------------
def test_moving_averages(db):
    """Prices 100,101,…: the 5-period MA at the 5th row is mean(100..104)=102."""
    seed_series(db, count=30, start_price=100.0, step=1.0)
    _build(db)
    rows = _gold_rows(db)

    assert len(rows) == 30
    assert rows[0][4] == pytest.approx(100.0)      # window of one
    assert rows[4][4] == pytest.approx(102.0)      # mean(100..104)
    assert rows[29][4] == pytest.approx(127.0)     # mean(125..129)
    assert rows[14][5] == pytest.approx(107.0)     # 15-period: mean(100..114)


def test_intraday_return_is_measured_from_the_session_open(db):
    seed_series(db, count=10, start_price=100.0, step=1.0)
    _build(db)
    rows = _gold_rows(db)

    assert rows[0][2] == pytest.approx(100.0)      # session_open
    assert rows[4][7] == pytest.approx(4.0)        # (104-100)/100*100
    assert rows[9][7] == pytest.approx(9.0)


def test_period_return_and_log_return(db):
    import math

    seed_series(db, count=5, start_price=100.0, step=1.0)
    _build(db)
    rows = _gold_rows(db)

    assert rows[0][3] is None                      # no previous observation
    assert rows[1][3] == pytest.approx(100.0)      # prev_price
    assert rows[1][8] == pytest.approx(1.0)        # (101-100)/100*100
    assert rows[1][9] == pytest.approx(math.log(101 / 100))


def test_vwap_equals_mean_price_when_volume_is_constant(db):
    seed_series(db, count=10, start_price=100.0, step=1.0, volume=1_000)
    _build(db)
    rows = _gold_rows(db)

    assert rows[4][12] == pytest.approx(102.0)     # mean(100..104)
    assert rows[9][12] == pytest.approx(104.5)     # mean(100..109)


def test_zscore_is_null_when_the_window_has_no_variance(db):
    """A constant series has zero stddev — the guard must yield NULL, not ∞."""
    seed_series(db, count=30, start_price=100.0, step=0.0, volume=1_000)
    _build(db)
    rows = _gold_rows(db)

    assert all(row[11] is None for row in rows)    # volume_z_score


def test_observation_index_counts_within_the_session(db):
    seed_series(db, count=6)
    _build(db)
    rows = _gold_rows(db)
    assert [row[13] for row in rows] == [1, 2, 3, 4, 5, 6]


def test_cumulative_volume_is_converted_to_a_per_observation_delta(db):
    """Live snapshots report session-to-date volume; Gold must difference it.

    The first reading of a session yields a delta of **zero**, by design. Its
    volume accrued before the engine started watching, so booking it as one
    interval's trade would fabricate an enormous false volume surge every time
    the pipeline starts mid-session — exactly the artefact the anomaly engine
    would then dutifully report.
    """
    for index, cumulative in enumerate([1_000, 2_500, 4_000]):
        insert_raw_quote(
            db, "TEST", START + timedelta(minutes=5 * index),
            price=100.0 + index, volume=cumulative, source="yfinance",
        )
    _build(db)
    rows = _gold_rows(db)

    assert [row[10] for row in rows] == [0, 1_500, 1_500]


def test_cumulative_volume_never_goes_negative_across_a_session_reset(db):
    """A counter that resets (new session) must clamp at zero, not go negative."""
    for index, cumulative in enumerate([5_000, 9_000, 200]):
        insert_raw_quote(
            db, "TEST", START + timedelta(minutes=5 * index),
            price=100.0, volume=cumulative, source="yfinance",
        )
    _build(db)
    deltas = [row[10] for row in _gold_rows(db)]

    assert deltas == [0, 4_000, 0]
    assert all(delta >= 0 for delta in deltas)


def test_bar_volume_is_passed_through_unchanged(db):
    for index, volume in enumerate([1_000, 2_500, 4_000]):
        insert_raw_quote(
            db, "TEST", START + timedelta(minutes=5 * index),
            price=100.0 + index, volume=volume, source="yfinance_history",
        )
    _build(db)
    assert [row[10] for row in _gold_rows(db)] == [1_000, 2_500, 4_000]


def test_transforms_are_idempotent(db):
    seed_series(db, count=20, step=1.0)
    _build(db)
    first = _gold_rows(db)
    _build(db)
    second = _gold_rows(db)

    assert len(first) == len(second) == 20
    assert [row[1] for row in first] == [row[1] for row in second]


# ---------------------------------------------------------------------------
# Gold — daily summary
# ---------------------------------------------------------------------------
def test_daily_summary_rollup(db):
    seed_series(db, count=10, start_price=100.0, step=1.0, volume=1_000)
    _build(db)

    observations, first_price, last_price, high, low, day_return, total_volume = db.execute(
        """
        SELECT observations, first_price, last_price, session_high, session_low,
               day_return_pct, total_volume
        FROM gold.daily_summary WHERE ticker = 'TEST'
        """
    ).fetchone()

    assert observations == 10
    assert first_price == pytest.approx(100.0)     # the session open
    assert last_price == pytest.approx(109.0)
    assert high == pytest.approx(109.0)
    assert low == pytest.approx(100.0)
    assert day_return == pytest.approx(9.0)
    assert total_volume == 10_000
