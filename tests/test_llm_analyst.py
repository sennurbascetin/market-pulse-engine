"""Phase 4B — the LLM Market Analyst against mocked model responses.

The model is untrusted input. These tests cover the three things that matter:
schema validation, the Platinum writes, and the guarantee that a failing
provider degrades to the offline analyst instead of breaking the run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from market_pulse_engine.config import CONFIG
from market_pulse_engine.intelligence import heuristic, llm_analyst
from market_pulse_engine.intelligence.llm_provider import (
    BaseProvider, LLMError, LLMResponse, OfflineProvider, get_provider,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------
class ScriptedProvider(BaseProvider):
    """Returns a canned payload and records the prompts it was given."""

    name = "scripted"
    model = "scripted-1"
    offline = False

    def __init__(self, payload, tokens: int = 42):
        self._payload = payload
        self._tokens = tokens
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> LLMResponse:
        self.calls.append((system, user))
        text = self._payload if isinstance(self._payload, str) else json.dumps(self._payload)
        return LLMResponse(text=text, tokens_used=self._tokens, model=self.model, provider=self.name)


class BrokenProvider(BaseProvider):
    name = "broken"
    model = "broken-1"
    offline = False

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> LLMResponse:
        raise LLMError("upstream exploded")


def _seed_article(conn, article_id="a1", title="Nvidia surges to record high on AI demand"):
    conn.execute(
        """
        INSERT INTO silver.news_articles
            (article_id, title, summary, url, source, published_at,
             tickers_mentioned, ingested_at, run_id)
        VALUES (?, ?, ?, ?, 'Test Feed', ?, ?, ?, 'r') ON CONFLICT DO NOTHING
        """,
        [article_id, title, "Shares rallied hard.", f"https://example.com/{article_id}",
         NOW, ["NVDA"], NOW],
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def test_as_json_handles_plain_fenced_and_prefixed_output():
    plain = LLMResponse(text='{"sentiment": "bullish"}')
    assert plain.as_json() == {"sentiment": "bullish"}

    fenced = LLMResponse(text='```json\n{"sentiment": "bearish"}\n```')
    assert fenced.as_json() == {"sentiment": "bearish"}

    chatty = LLMResponse(text='Here you go:\n{"sentiment": "neutral"}\nHope that helps!')
    assert chatty.as_json() == {"sentiment": "neutral"}


def test_as_json_rejects_unparseable_output():
    with pytest.raises(LLMError):
        LLMResponse(text="I'm afraid I can't do that.").as_json()
    with pytest.raises(LLMError):
        LLMResponse(text='{"broken": ').as_json()


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------
def test_validate_sentiment_accepts_a_well_formed_payload():
    record = llm_analyst.validate_sentiment(
        {
            "sentiment": "Bullish",
            "confidence": 0.82,
            "key_themes": ["AI Demand", "Earnings"],
            "tickers_impacted": ["nvda"],
            "rationale": "Guidance raised.",
        },
        [],
    )
    assert record["sentiment"] == "bullish"          # normalised
    assert record["confidence"] == 0.82
    assert record["key_themes"] == ["ai demand", "earnings"]
    assert record["tickers_impacted"] == ["NVDA"]    # upper-cased


def test_validate_sentiment_rejects_an_invalid_label():
    with pytest.raises(LLMError):
        llm_analyst.validate_sentiment({"sentiment": "very bullish", "confidence": 0.9}, [])
    with pytest.raises(LLMError):
        llm_analyst.validate_sentiment({"sentiment": "bullish"}, [])   # no confidence


def test_validate_sentiment_clamps_confidence_and_caps_themes():
    record = llm_analyst.validate_sentiment(
        {"sentiment": "bearish", "confidence": 4.7, "key_themes": ["a", "b", "c", "d", "e"]}, []
    )
    assert record["confidence"] == 1.0
    assert len(record["key_themes"]) == 3


def test_validate_sentiment_discards_tickers_outside_the_watchlist():
    """A hallucinated symbol must never reach the Platinum table."""
    record = llm_analyst.validate_sentiment(
        {"sentiment": "bullish", "confidence": 0.5, "tickers_impacted": ["NVDA", "FAKE", "ZZZZ"]},
        [],
    )
    assert record["tickers_impacted"] == ["NVDA"]


def test_validate_sentiment_falls_back_to_detected_tickers():
    record = llm_analyst.validate_sentiment(
        {"sentiment": "neutral", "confidence": 0.4, "tickers_impacted": []}, ["AAPL"]
    )
    assert record["tickers_impacted"] == ["AAPL"]


def test_validate_narrative():
    record = llm_analyst.validate_narrative(
        {"headline": "Risk on", "narrative": "Tape is firm.", "regime": "RISK_ON"}
    )
    assert record == {"headline": "Risk on", "narrative": "Tape is firm.", "regime": "risk_on"}

    # An unknown regime degrades to "mixed" rather than failing the run.
    assert llm_analyst.validate_narrative(
        {"narrative": "Something happened.", "regime": "sideways"}
    )["regime"] == "mixed"

    with pytest.raises(LLMError):
        llm_analyst.validate_narrative({"narrative": "   ", "regime": "calm"})


# ---------------------------------------------------------------------------
# Platinum writes
# ---------------------------------------------------------------------------
def test_scoring_writes_platinum_news_sentiment(db, run_id):
    _seed_article(db)
    provider = ScriptedProvider(
        {"sentiment": "bullish", "confidence": 0.9,
         "key_themes": ["ai demand"], "tickers_impacted": ["NVDA"],
         "rationale": "Guidance raised."}
    )

    result = llm_analyst.score_recent_news(run_id, provider)

    assert result.articles_scored == 1
    assert result.llm_calls == 1
    assert result.tokens_used == 42
    assert result.degraded == 0

    row = db.execute(
        """
        SELECT sentiment, confidence, key_themes, tickers_impacted, provider, model, scored_at
        FROM platinum.news_sentiment WHERE article_id = 'a1'
        """
    ).fetchone()
    assert row[0] == "bullish"
    assert row[1] == pytest.approx(0.9)
    assert list(row[2]) == ["ai demand"]
    assert list(row[3]) == ["NVDA"]
    assert row[4] == "scripted"
    assert row[5] == "scripted-1"
    assert row[6].tzinfo is not None


def test_already_scored_articles_are_not_rescored(db, run_id):
    _seed_article(db)
    provider = ScriptedProvider({"sentiment": "neutral", "confidence": 0.5})

    llm_analyst.score_recent_news(run_id, provider)
    second = llm_analyst.score_recent_news(run_id, provider)

    assert second.articles_scored == 0
    assert db.execute("SELECT count(*) FROM platinum.news_sentiment").fetchone()[0] == 1


def test_a_failing_provider_degrades_to_the_offline_analyst(db, run_id):
    """The pipeline must keep producing insight when the vendor is down."""
    _seed_article(db)

    result = llm_analyst.score_recent_news(run_id, BrokenProvider())

    assert result.articles_scored == 1        # still written
    assert result.llm_calls == 0
    assert result.degraded == 1

    sentiment, provider, model = db.execute(
        "SELECT sentiment, provider, model FROM platinum.news_sentiment WHERE article_id = 'a1'"
    ).fetchone()
    assert sentiment in {"bullish", "bearish", "neutral"}
    assert provider == "heuristic"
    assert model == "rule-based-analyst"


def test_malformed_model_output_also_degrades(db, run_id):
    _seed_article(db)
    result = llm_analyst.score_recent_news(run_id, ScriptedProvider("not json at all"))

    assert result.articles_scored == 1
    assert result.degraded == 1


def test_narrative_is_written_with_its_context(db, run_id):
    provider = ScriptedProvider(
        {"headline": "Risk on", "narrative": "NVDA leads on volume.", "regime": "risk_on"},
        tokens=128,
    )
    record, tokens, degraded = llm_analyst.generate_market_pulse(run_id, provider)

    assert record["regime"] == "risk_on"
    assert tokens == 128
    assert degraded is False

    headline, narrative, regime, provider_name, stored_tokens, context = db.execute(
        """
        SELECT headline, narrative, regime, provider, tokens_used, context
        FROM platinum.market_pulse_narratives
        """
    ).fetchone()
    assert headline == "Risk on"
    assert narrative == "NVDA leads on volume."
    assert regime == "risk_on"
    assert provider_name == "scripted"
    assert stored_tokens == 128
    # The exact facts shown to the analyst are stored alongside its answer.
    assert set(json.loads(context)) >= {"tickers", "anomalies", "top_news", "fear_greed"}


def test_narrative_degrades_when_the_provider_fails(db, run_id):
    record, tokens, degraded = llm_analyst.generate_market_pulse(run_id, BrokenProvider())

    assert degraded is True
    assert tokens == 0
    assert record["narrative"]
    assert db.execute(
        "SELECT provider FROM platinum.market_pulse_narratives"
    ).fetchone()[0] == "heuristic"


def test_prompt_contains_the_watchlist_and_the_headline(db, run_id):
    _seed_article(db)
    provider = ScriptedProvider({"sentiment": "neutral", "confidence": 0.5})
    llm_analyst.score_recent_news(run_id, provider)

    _system, user = provider.calls[0]
    assert "Nvidia surges" in user
    assert "NVDA" in user


# ---------------------------------------------------------------------------
# Offline analyst
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Nvidia surges to record high on blowout demand", "bullish"),
        ("Tesla plunges after deliveries miss estimates", "bearish"),
        ("Microsoft announces quarterly dividend", "neutral"),
    ],
)
def test_offline_sentiment_directions(headline, expected):
    assert heuristic.score_sentiment(headline, None)["sentiment"] == expected


def test_offline_sentiment_handles_negation():
    negated = heuristic.score_sentiment("Apple does not expect growth to rebound", None)
    plain = heuristic.score_sentiment("Apple expects growth to rebound", None)
    assert plain["sentiment"] == "bullish"
    assert negated["sentiment"] != "bullish"


def test_negation_lookup_is_word_bounded():
    """"McKinnon" contains "no" — it must not invert the sentence's polarity."""
    record = heuristic.score_sentiment("Columbus McKinnon delivers 66% return", None)
    assert record["sentiment"] == "bullish"


def test_theme_detection_is_word_bounded():
    """"email" contains "ai" — it must not be filed under artificial intelligence."""
    themes = heuristic.detect_themes("i got two email invitations from friends")
    assert "artificial intelligence" not in themes


def test_offline_narrative_uses_only_supplied_facts():
    context = {
        "tickers": [
            {"ticker": "NVDA", "price": 223.96, "intraday_return_pct": 1.07},
            {"ticker": "MSFT", "price": 499.99, "intraday_return_pct": -0.16},
        ],
        "anomalies": [
            {"ticker": "NVDA", "anomaly_type": "volume_surge", "severity": "high", "z_score": 6.7}
        ],
        "top_news": [{"sentiment": "bullish"}, {"sentiment": "bullish"}, {"sentiment": "neutral"}],
        "fear_greed": {"score": 63.7, "label": "Greed"},
    }
    record = heuristic.compose_narrative(context)

    assert record["regime"] in {"risk_on", "risk_off", "mixed", "calm"}
    assert "NVDA" in record["narrative"]
    assert "6.7σ" in record["narrative"]
    assert "64 (Greed)" in record["narrative"]


def test_regime_classification():
    up = {"tickers": [{"ticker": "A", "intraday_return_pct": 1.5}], "fear_greed": {"score": 70}}
    down = {"tickers": [{"ticker": "A", "intraday_return_pct": -1.5}], "fear_greed": {"score": 20}}
    flat = {"tickers": [{"ticker": "A", "intraday_return_pct": 0.01}], "anomalies": []}

    assert heuristic.classify_regime(up) == "risk_on"
    assert heuristic.classify_regime(down) == "risk_off"
    assert heuristic.classify_regime(flat) == "calm"


# ---------------------------------------------------------------------------
# Provider resolution
# ---------------------------------------------------------------------------
def test_forcing_heuristic_returns_the_offline_provider():
    provider = get_provider("heuristic")
    assert isinstance(provider, OfflineProvider)
    assert provider.offline is True


def test_missing_credentials_fall_back_offline(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert get_provider("openai").offline is True
    assert get_provider("auto").offline is True


def test_offline_provider_refuses_completions():
    with pytest.raises(LLMError):
        OfflineProvider().complete("system", "user")


def test_full_analyst_pass_writes_both_platinum_tables(db, run_id):
    _seed_article(db)
    result = llm_analyst.run(run_id, get_provider("heuristic"))

    assert result.articles_scored == 1
    assert result.narrative is not None
    assert db.execute("SELECT count(*) FROM platinum.news_sentiment").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM platinum.market_pulse_narratives").fetchone()[0] == 1
