"""Phase 2 — ingestion connectors against mocked APIs.

Every external call is replaced with a canned response, so the suite is fast,
offline and deterministic. What is asserted is Bronze schema compliance and
idempotency, which is what the connectors are actually responsible for.
"""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone

import pytest

from market_pulse_engine.ingestion import news_fetcher, price_fetcher, sentiment_fetcher

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
SAMPLE_INFO = {
    "currentPrice": 223.96,
    "regularMarketPrice": 223.96,
    "open": 221.59,
    "dayHigh": 224.76,
    "dayLow": 220.66,
    "previousClose": 220.10,
    "volume": 34_331_108,
    "marketCap": 5_400_000_000_000,
    "fiftyDayAverage": 210.5,
    "twoHundredDayAverage": 190.25,
    "currency": "USD",
    "exchange": "NMS",
    "marketState": "CLOSED",
    "regularMarketTime": 1786132801,
}

RSS_BODY = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Nvidia surges to record high on blowout data center demand</title>
    <description>Shares rallied after guidance was raised.</description>
    <link>https://example.com/nvda-surge</link>
    <pubDate>Sat, 08 Aug 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Tesla plunges after deliveries miss estimates</title>
    <link>https://example.com/tsla-miss</link>
    <pubDate>Sat, 08 Aug 2026 13:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

FEAR_GREED_BODY = {
    "fear_and_greed": {
        "score": 63.6857142857143,
        "rating": "greed",
        "timestamp": "2026-08-07T23:59:47+00:00",
        "previous_close": 59.71,
        "previous_1_week": 45.22,
        "previous_1_month": 39.77,
    },
    "market_momentum_sp500": {"score": 71.2, "rating": "greed"},
}


class _FakeResponse:
    def __init__(self, content=b"", payload=None, status=200):
        self.content = content
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def fake_yfinance(monkeypatch):
    """Install a stand-in ``yfinance`` module for the lazily-imported connector."""

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            return dict(SAMPLE_INFO)

        @property
        def fast_info(self):
            return {"lastPrice": SAMPLE_INFO["currentPrice"]}

    module = types.ModuleType("yfinance")
    module.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", module)
    return module


# ---------------------------------------------------------------------------
# 2A — prices
# ---------------------------------------------------------------------------
def test_fetch_quotes_writes_bronze_schema(db, run_id, fake_yfinance):
    result = price_fetcher.fetch_quotes(run_id, ["NVDA", "AAPL"])

    assert result.written == 2
    assert result.errors == []

    rows = db.execute(
        """
        SELECT quote_id, run_id, ticker, source, observed_at, ingested_at, is_live, payload
        FROM bronze.raw_quotes ORDER BY ticker
        """
    ).fetchall()
    assert len(rows) == 2

    for quote_id, row_run_id, ticker, source, observed_at, ingested_at, is_live, payload in rows:
        assert len(quote_id) == 32                    # deterministic sha256 prefix
        assert row_run_id == run_id
        assert ticker in {"NVDA", "AAPL"}
        assert source == "yfinance"
        assert observed_at.tzinfo is not None         # Bronze is always tz-aware
        assert ingested_at.tzinfo is not None
        assert isinstance(is_live, bool)
        parsed = json.loads(payload)
        assert parsed["currentPrice"] == pytest.approx(223.96)


def test_fetch_quotes_is_idempotent(db, run_id, fake_yfinance, monkeypatch):
    """Re-ingesting an unchanged observation must not create a second row."""
    # Freeze the clock so both passes derive the same observed_at.
    frozen = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(price_fetcher, "utcnow", lambda: frozen)
    monkeypatch.setattr(price_fetcher, "is_live_for", lambda *_args, **_kw: False)

    first = price_fetcher.fetch_quotes(run_id, ["NVDA"])
    second = price_fetcher.fetch_quotes(run_id, ["NVDA"])

    assert first.written == 1
    assert second.written == 0
    assert second.duplicates == 1
    assert db.execute("SELECT count(*) FROM bronze.raw_quotes").fetchone()[0] == 1


def test_quote_without_price_is_reported_not_written(db, run_id, monkeypatch):
    monkeypatch.setattr(price_fetcher, "_snapshot", lambda _ticker: None)
    result = price_fetcher.fetch_quotes(run_id, ["NVDA"])

    assert result.written == 0
    assert result.errors and "NVDA" in result.errors[0]


def test_backfill_marks_history_as_not_live(db, run_id, monkeypatch):
    pandas = pytest.importorskip("pandas")
    index = pandas.date_range("2026-08-07 14:00", periods=3, freq="5min", tz="UTC")
    frame = pandas.DataFrame(
        {"Open": [100.0, 101.0, 102.0], "High": [101.0, 102.0, 103.0],
         "Low": [99.0, 100.0, 101.0], "Close": [100.5, 101.5, 102.5],
         "Volume": [1000, 1100, 1200]},
        index=index,
    )

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **_kwargs):
            return frame

    module = types.ModuleType("yfinance")
    module.Ticker = FakeTicker
    monkeypatch.setitem(sys.modules, "yfinance", module)

    result = price_fetcher.backfill_history(run_id, ["NVDA"])

    assert result.written == 3
    sources = db.execute(
        "SELECT DISTINCT source, is_live FROM bronze.raw_quotes"
    ).fetchall()
    assert sources == [("yfinance_history", False)]


# ---------------------------------------------------------------------------
# 2B — news
# ---------------------------------------------------------------------------
def test_fetch_news_parses_and_attributes_tickers(db, run_id, monkeypatch):
    monkeypatch.setattr(
        news_fetcher.requests, "get", lambda *_a, **_kw: _FakeResponse(content=RSS_BODY)
    )
    feeds = [{"name": "Test Feed", "url": "https://example.com/rss"}]
    result = news_fetcher.fetch_news(run_id, feeds)

    assert result.written == 2

    rows = db.execute(
        "SELECT article_id, feed_name, payload FROM bronze.raw_news ORDER BY article_id"
    ).fetchall()
    payloads = [json.loads(row[2]) for row in rows]
    by_title = {payload["title"]: payload for payload in payloads}

    nvda = next(p for t, p in by_title.items() if "Nvidia" in t)
    assert "NVDA" in nvda["tickers_mentioned"]
    assert nvda["summary"] == "Shares rallied after guidance was raised."

    # The second item has no <description>; the title carries through.
    tsla = next(p for t, p in by_title.items() if "Tesla" in t)
    assert "TSLA" in tsla["tickers_mentioned"]
    assert tsla["summary"] == tsla["title"]


def test_news_deduplicates_by_url(db, run_id, monkeypatch):
    monkeypatch.setattr(
        news_fetcher.requests, "get", lambda *_a, **_kw: _FakeResponse(content=RSS_BODY)
    )
    feeds = [{"name": "Test Feed", "url": "https://example.com/rss"}]

    news_fetcher.fetch_news(run_id, feeds)
    second = news_fetcher.fetch_news(run_id, feeds)

    assert second.written == 0
    assert db.execute("SELECT count(*) FROM bronze.raw_news").fetchone()[0] == 2


def test_failing_feed_is_recorded_not_raised(db, run_id, monkeypatch):
    def explode(*_args, **_kwargs):
        raise ConnectionError("feed down")

    monkeypatch.setattr(news_fetcher.requests, "get", explode)
    result = news_fetcher.fetch_news(run_id, [{"name": "Broken", "url": "https://x/rss"}])

    assert result.written == 0
    assert len(result.errors) == 1
    assert not result.ok


def test_extract_tickers_uses_word_boundaries():
    """Aliases must not match inside longer words."""
    assert news_fetcher.extract_tickers("Apple beats estimates") == ["AAPL"]
    assert "ETH-USD" not in news_fetcher.extract_tickers("Something method-related")


# ---------------------------------------------------------------------------
# 2C — sentiment index
# ---------------------------------------------------------------------------
def test_fetch_sentiment_index_writes_bronze(db, run_id, monkeypatch):
    monkeypatch.setattr(
        sentiment_fetcher.requests, "get",
        lambda *_a, **_kw: _FakeResponse(payload=FEAR_GREED_BODY),
    )
    result = sentiment_fetcher.fetch_sentiment_index(run_id)

    assert result.written == 1
    reading_id, source, observed_at, payload = db.execute(
        "SELECT reading_id, source, observed_at, payload FROM bronze.raw_sentiment_index"
    ).fetchone()

    assert source == "cnn_fear_greed"
    assert observed_at == datetime(2026, 8, 7, 23, 59, 47, tzinfo=timezone.utc)
    parsed = json.loads(payload)
    assert parsed["score"] == pytest.approx(63.6857142857143)
    assert parsed["rating"] == "Greed"                     # normalised to title case
    assert "market_momentum_sp500" in parsed["components"]


def test_sentiment_endpoint_failure_degrades_gracefully(db, run_id, monkeypatch):
    """The CNN endpoint is unofficial — a failure must never raise."""
    def explode(*_args, **_kwargs):
        raise TimeoutError("cnn unreachable")

    monkeypatch.setattr(sentiment_fetcher.requests, "get", explode)
    result = sentiment_fetcher.fetch_sentiment_index(run_id)

    assert result.written == 0
    assert not result.ok
    assert db.execute("SELECT count(*) FROM bronze.raw_sentiment_index").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("score", "expected"),
    [(10, "Extreme Fear"), (30, "Fear"), (50, "Neutral"), (60, "Greed"), (90, "Extreme Greed")],
)
def test_label_bands(score, expected):
    assert sentiment_fetcher.label_for(score) == expected
