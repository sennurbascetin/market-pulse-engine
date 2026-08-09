"""Sub-module 4B — the LLM Market Analyst.

Two jobs per pipeline run:

1. **News sentiment scoring** — the most recent unscored articles from
   ``silver.news_articles`` are classified into a strict JSON schema and written
   to ``platinum.news_sentiment``.
2. **Market Pulse narrative** — one briefing per run, assembled from the facts
   the pipeline actually measured (prices, anomalies, top sentiment, Fear &
   Greed) and written to ``platinum.market_pulse_narratives``.

Every remote call is defensive. A failed request, a malformed response, or a
missing key degrades that single item to the offline analyst rather than failing
the run: the pipeline must never stop producing insight because a vendor is
having a bad afternoon.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..config import CONFIG
from ..db.connection import get_connection, transaction
from ..logging_setup import get_logger
from ..utils import ensure_utc, stable_id, to_float, truncate, utcnow
from . import heuristic
from .llm_provider import BaseProvider, LLMError, get_provider

log = get_logger("intelligence.analyst")

VALID_SENTIMENTS = ("bullish", "bearish", "neutral")
VALID_REGIMES = ("risk_on", "risk_off", "mixed", "calm")

SENTIMENT_SYSTEM_PROMPT = """You are a sell-side financial news analyst.
Classify the market sentiment of ONE news item.

Rules:
- Judge the impact on equity/crypto prices, not whether the news is pleasant.
- "neutral" is a real answer: use it for routine or purely descriptive coverage.
- confidence reflects how clear the directional signal is, never certainty in the outcome.
- tickers_impacted may ONLY contain symbols from the provided watchlist. Empty is fine.
- key_themes: at most 3 short lower-case noun phrases.

Respond with a single JSON object and nothing else:
{"sentiment": "bullish|bearish|neutral", "confidence": 0.0-1.0,
 "key_themes": ["..."], "tickers_impacted": ["..."], "rationale": "one short sentence"}"""

NARRATIVE_SYSTEM_PROMPT = """You are the market strategist who writes the "Market Pulse" —
a terse briefing shown at the top of a live trading dashboard.

Rules:
- 2-3 sentences. No preamble, no sign-off, no bullet points.
- Explain WHY the tape looks the way it does, connecting price action to the
  anomalies and news supplied. Do not simply restate the numbers.
- Use ONLY the facts in the data block. Never invent an event, a figure or a
  cause. If the data is thin, say the tape is quiet.
- Quote sigma values and percentages exactly as given.
- regime: risk_on if breadth and sentiment are constructive, risk_off if
  defensive, calm if genuinely uneventful, otherwise mixed.

Respond with a single JSON object and nothing else:
{"headline": "under six words", "narrative": "the 2-3 sentence briefing",
 "regime": "risk_on|risk_off|mixed|calm"}"""


@dataclass
class AnalystResult:
    """What one intelligence pass produced, for the run log."""

    provider: str
    model: str
    articles_scored: int = 0
    llm_calls: int = 0
    tokens_used: int = 0
    narrative: dict[str, Any] | None = None
    degraded: int = 0  # items that fell back to the offline analyst
    errors: list[str] = field(default_factory=list)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "articles_scored": self.articles_scored,
            "llm_calls": self.llm_calls,
            "llm_tokens_used": self.tokens_used,
            "degraded": self.degraded,
        }


# ---------------------------------------------------------------------------
# Schema validation — the LLM is untrusted input
# ---------------------------------------------------------------------------
def validate_sentiment(payload: dict[str, Any], fallback_tickers: list[str]) -> dict[str, Any]:
    """Coerce a model response into the ``platinum.news_sentiment`` contract.

    Raises :class:`LLMError` when the response cannot be repaired, so the caller
    can fall back to the offline analyst.
    """
    sentiment = str(payload.get("sentiment", "")).strip().lower()
    if sentiment not in VALID_SENTIMENTS:
        raise LLMError(f"invalid sentiment {sentiment!r}")

    confidence = to_float(payload.get("confidence"))
    if confidence is None:
        raise LLMError("missing confidence")
    confidence = max(0.0, min(1.0, confidence))

    themes = payload.get("key_themes") or []
    if not isinstance(themes, list):
        themes = [str(themes)]
    key_themes = [str(theme).strip().lower()[:60] for theme in themes if str(theme).strip()][:3]

    impacted = payload.get("tickers_impacted") or []
    if not isinstance(impacted, list):
        impacted = [str(impacted)]
    watchlist = {ticker.upper() for ticker in CONFIG.watchlist}
    # The model is told to stay inside the watchlist; enforce it rather than trust it.
    tickers_impacted = sorted({str(t).strip().upper() for t in impacted} & watchlist)
    if not tickers_impacted:
        tickers_impacted = sorted(set(fallback_tickers) & watchlist)

    return {
        "sentiment": sentiment,
        "confidence": round(confidence, 3),
        "key_themes": key_themes or ["general market"],
        "tickers_impacted": tickers_impacted,
        "rationale": truncate(str(payload.get("rationale") or ""), 300) or "",
    }


def validate_narrative(payload: dict[str, Any]) -> dict[str, str]:
    """Coerce a model response into the narrative contract."""
    narrative = truncate(str(payload.get("narrative") or "").strip(), 900)
    if not narrative:
        raise LLMError("empty narrative")
    regime = str(payload.get("regime", "")).strip().lower()
    if regime not in VALID_REGIMES:
        regime = "mixed"
    headline = truncate(str(payload.get("headline") or "Market Pulse").strip(), 80) or "Market Pulse"
    return {"headline": headline, "narrative": narrative, "regime": regime}


# ---------------------------------------------------------------------------
# Context assembly — the exact facts the analyst is shown
# ---------------------------------------------------------------------------
def build_context(run_id: str | None = None, *, anomaly_limit: int = 8, news_limit: int = 3) -> dict[str, Any]:
    """Gather the current market picture from Gold and Platinum."""
    conn = get_connection()

    tickers = conn.execute(
        """
        SELECT ticker, price, intraday_return_pct, volume_z_score, is_live, observed_at
        FROM gold.quotes_enriched
        QUALIFY row_number() OVER (PARTITION BY ticker ORDER BY observed_at DESC) = 1
        ORDER BY ticker
        """
    ).fetchall()

    anomalies = conn.execute(
        """
        SELECT ticker, anomaly_type, severity, z_score, description, observed_at
        FROM platinum.anomalies
        ORDER BY observed_at DESC, abs(z_score) DESC
        LIMIT ?
        """,
        [anomaly_limit],
    ).fetchall()

    top_news = conn.execute(
        """
        SELECT a.title, s.sentiment, s.confidence, a.source, s.key_themes, s.tickers_impacted
        FROM platinum.news_sentiment s
        JOIN silver.news_articles a USING (article_id)
        ORDER BY s.scored_at DESC, s.confidence DESC
        LIMIT ?
        """,
        [news_limit],
    ).fetchall()

    fear_greed_row = conn.execute(
        "SELECT score, label, observed_at FROM silver.sentiment_index ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()

    from ..ingestion.market_hours import session_state

    return {
        "generated_at": utcnow().isoformat(),
        "session_state": session_state(),
        "run_id": run_id,
        "tickers": [
            {
                "ticker": row[0],
                "price": to_float(row[1]),
                "intraday_return_pct": to_float(row[2]),
                "volume_z_score": to_float(row[3]),
                "is_live": bool(row[4]),
            }
            for row in tickers
        ],
        "anomalies": [
            {
                "ticker": row[0],
                "anomaly_type": row[1],
                "severity": row[2],
                "z_score": to_float(row[3]),
                "description": row[4],
            }
            for row in anomalies
        ],
        "top_news": [
            {
                "title": row[0],
                "sentiment": row[1],
                "confidence": to_float(row[2]),
                "source": row[3],
                "key_themes": list(row[4] or []),
                "tickers_impacted": list(row[5] or []),
            }
            for row in top_news
        ],
        "fear_greed": (
            {"score": to_float(fear_greed_row[0]), "label": fear_greed_row[1]}
            if fear_greed_row
            else None
        ),
    }


# ---------------------------------------------------------------------------
# 1. News sentiment scoring
# ---------------------------------------------------------------------------
def _unscored_articles(limit: int) -> list[dict[str, Any]]:
    """Most recent articles that have not yet been through the analyst."""
    rows = get_connection().execute(
        """
        SELECT a.article_id, a.title, a.summary, a.tickers_mentioned, a.source
        FROM silver.news_articles a
        LEFT JOIN platinum.news_sentiment s USING (article_id)
        WHERE s.article_id IS NULL
        ORDER BY COALESCE(a.published_at, a.ingested_at) DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        {
            "article_id": row[0],
            "title": row[1],
            "summary": row[2],
            "tickers_mentioned": list(row[3] or []),
            "source": row[4],
        }
        for row in rows
    ]


def _score_one(provider: BaseProvider, article: dict[str, Any]) -> tuple[dict[str, Any], int, bool]:
    """Score a single article. Returns ``(record, tokens, degraded)``."""
    if provider.offline:
        return (
            heuristic.score_sentiment(article["title"], article["summary"], article["tickers_mentioned"]),
            0,
            True,
        )

    user_prompt = json.dumps(
        {
            "watchlist": CONFIG.watchlist,
            "headline": article["title"],
            "summary": article["summary"],
            "source": article["source"],
            "symbols_detected_in_text": article["tickers_mentioned"],
        },
        indent=2,
    )

    try:
        response = provider.complete(SENTIMENT_SYSTEM_PROMPT, user_prompt, json_mode=True)
        record = validate_sentiment(response.as_json(), article["tickers_mentioned"])
        return record, response.tokens_used, False
    except (LLMError, Exception) as exc:  # noqa: BLE001 - never fail the run on one article
        log.warning(
            "sentiment scoring degraded to offline analyst",
            extra={"article_id": article["article_id"], "error": str(exc)[:200]},
        )
        return (
            heuristic.score_sentiment(article["title"], article["summary"], article["tickers_mentioned"]),
            0,
            True,
        )


def score_recent_news(run_id: str, provider: BaseProvider, limit: int | None = None) -> AnalystResult:
    """Score unprocessed articles and write them to ``platinum.news_sentiment``."""
    result = AnalystResult(provider=provider.name, model=provider.model)
    articles = _unscored_articles(limit or CONFIG.llm.max_articles_per_run)
    if not articles:
        return result

    # Remote scoring is network-bound and independent per article.
    workers = 1 if provider.offline else min(4, len(articles))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        scored = list(pool.map(lambda article: _score_one(provider, article), articles))

    scored_at = utcnow()
    rows: list[list[Any]] = []
    for article, (record, tokens, degraded) in zip(articles, scored):
        result.tokens_used += tokens
        result.degraded += int(degraded)
        if not degraded:
            result.llm_calls += 1
        rows.append(
            [
                article["article_id"],
                run_id,
                record["sentiment"],
                record["confidence"],
                record["key_themes"],
                record["tickers_impacted"],
                record["rationale"],
                "rule-based-analyst" if degraded else provider.model,
                "heuristic" if degraded else provider.name,
                scored_at,
            ]
        )

    with transaction() as conn:
        conn.executemany(
            """
            INSERT INTO platinum.news_sentiment (
                article_id, run_id, sentiment, confidence, key_themes,
                tickers_impacted, rationale, model, provider, scored_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            rows,
        )

    result.articles_scored = len(rows)
    log.info("news sentiment scored", extra=result.as_log_fields() | {"run_id": run_id})
    return result


# ---------------------------------------------------------------------------
# 2. Market Pulse narrative
# ---------------------------------------------------------------------------
def generate_market_pulse(
    run_id: str, provider: BaseProvider, context: dict[str, Any] | None = None
) -> tuple[dict[str, Any], int, bool]:
    """Produce and persist one Market Pulse briefing.

    Returns ``(narrative_record, tokens_used, degraded)``.
    """
    context = context or build_context(run_id)
    degraded = True
    tokens = 0

    if provider.offline:
        record = heuristic.compose_narrative(context)
    else:
        try:
            response = provider.complete(
                NARRATIVE_SYSTEM_PROMPT, json.dumps(context, indent=2, default=str), json_mode=True
            )
            record = validate_narrative(response.as_json())
            tokens = response.tokens_used
            degraded = False
        except (LLMError, Exception) as exc:  # noqa: BLE001
            log.warning("narrative degraded to offline analyst", extra={"error": str(exc)[:200]})
            record = heuristic.compose_narrative(context)

    generated_at = utcnow()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO platinum.market_pulse_narratives (
                narrative_id, run_id, generated_at, headline, narrative,
                regime, model, provider, tokens_used, context
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::JSON) ON CONFLICT DO NOTHING
            """,
            [
                stable_id(run_id, generated_at.isoformat()),
                run_id,
                generated_at,
                record["headline"],
                record["narrative"],
                record["regime"],
                "rule-based-analyst" if degraded else provider.model,
                "heuristic" if degraded else provider.name,
                tokens,
                json.dumps(context, default=str),
            ],
        )

    log.info(
        "market pulse generated",
        extra={
            "run_id": run_id,
            "regime": record["regime"],
            "provider": "heuristic" if degraded else provider.name,
            "llm_tokens_used": tokens,
        },
    )
    return record, tokens, degraded


def run(run_id: str, provider: BaseProvider | None = None) -> AnalystResult:
    """Run the full intelligence pass: score news, then write the briefing."""
    analyst = provider or get_provider()
    result = score_recent_news(run_id, analyst)

    context = build_context(run_id)
    narrative, tokens, degraded = generate_market_pulse(run_id, analyst, context)

    result.narrative = narrative
    result.tokens_used += tokens
    result.degraded += int(degraded)
    if not degraded:
        result.llm_calls += 1
    return result


def latest_narrative() -> dict[str, Any] | None:
    """The most recent briefing, for the dashboard."""
    row = get_connection().execute(
        """
        SELECT headline, narrative, regime, provider, model, generated_at
        FROM platinum.market_pulse_narratives
        ORDER BY generated_at DESC LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return {
        "headline": row[0],
        "narrative": row[1],
        "regime": row[2],
        "provider": row[3],
        "model": row[4],
        "generated_at": ensure_utc(row[5]),
    }
