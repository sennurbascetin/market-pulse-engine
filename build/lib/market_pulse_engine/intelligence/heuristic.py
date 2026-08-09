"""The offline analyst — deterministic sentiment scoring and narrative writing.

This is the fallback that makes the intelligence layer work with no API key, no
network and no cost. It is not a stub: it emits exactly the records the remote
analysts emit, so ``platinum.news_sentiment`` and
``platinum.market_pulse_narratives`` are populated identically either way.

Approach: a weighted financial-sentiment lexicon over the headline and summary,
with negation handling, plus a theme classifier. The narrative is assembled from
the same facts a remote model would be shown, so the briefing states only things
the pipeline actually measured.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Lexicons. Weights are magnitudes of conviction, not probabilities.
# ---------------------------------------------------------------------------
BULLISH_TERMS: dict[str, float] = {
    "surge": 2.0, "soar": 2.0, "rally": 2.0, "skyrocket": 2.5, "jump": 1.5,
    "climb": 1.2, "gain": 1.2, "rise": 1.0, "advance": 1.0, "recover": 1.2,
    "rebound": 1.5, "beat": 1.8, "beats": 1.8, "upgrade": 1.8, "outperform": 1.8,
    "record high": 2.5, "all-time high": 2.5, "breakout": 1.8, "bullish": 2.0,
    "optimism": 1.5, "strong": 1.2, "stronger": 1.3, "growth": 1.2, "profit": 1.2,
    "boost": 1.4, "boosts": 1.4, "expand": 1.0, "momentum": 1.2, "upside": 1.5,
    "buyback": 1.3, "dividend hike": 1.5, "raises guidance": 2.2, "top": 0.8,
    "tops": 1.5, "delivers": 1.2, "returns": 1.0, "return": 1.0, "gains": 1.4,
    "rallies": 1.8, "surges": 2.0, "soars": 2.0, "jumps": 1.5, "climbs": 1.2,
    "higher": 1.1, "outperformed": 1.8, "raised": 1.3, "raises": 1.3,
    "wins": 1.3, "approval": 1.2, "milestone": 1.0, "demand": 0.9,
}

BEARISH_TERMS: dict[str, float] = {
    "plunge": 2.2, "plummet": 2.4, "crash": 2.5, "slump": 1.8, "tumble": 1.8,
    "sink": 1.6, "slide": 1.4, "fall": 1.2, "drop": 1.3, "decline": 1.2,
    "miss": 1.8, "misses": 1.8, "downgrade": 1.8, "underperform": 1.8,
    "bearish": 2.0, "selloff": 2.0, "sell-off": 2.0, "warning": 1.5, "warns": 1.5,
    "loss": 1.4, "losses": 1.4, "weak": 1.3, "weaker": 1.4, "layoff": 1.6,
    "layoffs": 1.6, "recession": 2.0, "cut": 1.2, "cuts": 1.2, "slash": 1.8,
    "lawsuit": 1.3, "probe": 1.2, "investigation": 1.3, "fear": 1.5, "risk": 0.8,
    "downside": 1.5, "bankruptcy": 2.5, "default": 2.0, "halt": 1.4,
    "falls": 1.2, "drops": 1.3, "sinks": 1.6, "slides": 1.4, "tumbles": 1.8,
    "plunges": 2.2, "slumps": 1.8, "lower": 1.1, "lowered": 1.3, "scam": 1.2,
    "fraud": 2.0, "shortfall": 1.6, "delay": 1.1, "delays": 1.1, "stalls": 1.3,
}

#: Terms that invert the polarity of a match within the next few words.
NEGATIONS = ("not", "no", "never", "without", "fails to", "failed to", "unlikely to")

#: theme label -> trigger terms
THEMES: dict[str, tuple[str, ...]] = {
    "earnings": ("earnings", "revenue", "guidance", "quarter", "eps", "profit", "results"),
    "monetary policy": ("fed", "federal reserve", "rate", "inflation", "cpi", "powell", "yields"),
    "artificial intelligence": ("ai", "artificial intelligence", "chips", "gpu", "data center", "nvidia"),
    "crypto": ("bitcoin", "crypto", "ethereum", "blockchain", "token", "etf"),
    "regulation": ("regulator", "antitrust", "lawsuit", "probe", "sec ", "ruling", "fine"),
    "energy": ("oil", "crude", "opec", "gas", "energy", "barrel"),
    "labour market": ("jobs", "payroll", "unemployment", "hiring", "layoff"),
    "geopolitics": ("tariff", "sanction", "war", "election", "trade deal", "conflict"),
    "mergers & acquisitions": ("acquire", "acquisition", "merger", "takeover", "buyout", "stake"),
    "consumer demand": ("sales", "demand", "consumer", "spending", "retail"),
}

SENTIMENT_NEUTRAL_BAND = 1.0


def _matches(text: str, terms: dict[str, float]) -> list[tuple[str, float]]:
    """Find lexicon hits, discounting any that sit behind a negation."""
    found: list[tuple[str, float]] = []
    for term, weight in terms.items():
        for match in re.finditer(r"\b" + re.escape(term) + r"\b", text):
            window = text[max(0, match.start() - 24) : match.start()]
            # Word-boundary anchored: a substring test would find "no" inside
            # "McKinnon" and invert the polarity of a perfectly bullish headline.
            negated = any(_contains_word(window, negation) for negation in NEGATIONS)
            found.append((term, -weight * 0.6 if negated else weight))
    return found


def detect_themes(text: str, limit: int = 3) -> list[str]:
    """Return up to ``limit`` themes present in the text, most-evidenced first.

    Matching is word-boundary anchored: a bare substring test would find "ai"
    inside "email" and file a phishing story under artificial intelligence.
    """
    scores: dict[str, int] = {}
    for theme, triggers in THEMES.items():
        hits = sum(1 for trigger in triggers if _contains_word(text, trigger))
        if hits:
            scores[theme] = hits
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [theme for theme, _ in ranked[:limit]] or ["general market"]


def _contains_word(text: str, term: str) -> bool:
    """Word-boundary containment test for a possibly multi-word term."""
    return re.search(r"\b" + re.escape(term.strip()) + r"\b", text) is not None


def score_sentiment(title: str, summary: str | None, tickers: list[str] | None = None) -> dict[str, Any]:
    """Score one article, returning the same schema the remote analysts return."""
    text = f"{title} {summary or ''}".lower()

    bullish = _matches(text, BULLISH_TERMS)
    bearish = _matches(text, BEARISH_TERMS)
    bull_score = sum(weight for _, weight in bullish)
    bear_score = sum(weight for _, weight in bearish)
    net = bull_score - bear_score

    if net > SENTIMENT_NEUTRAL_BAND:
        sentiment = "bullish"
    elif net < -SENTIMENT_NEUTRAL_BAND:
        sentiment = "bearish"
    else:
        sentiment = "neutral"

    # Confidence rises with the margin between the two sides and with the amount
    # of evidence, and is capped short of certainty — this is a lexicon, not an
    # analyst.
    evidence = len(bullish) + len(bearish)
    margin = abs(net)
    if sentiment == "neutral":
        confidence = round(min(0.55, 0.30 + 0.04 * evidence), 2)
    else:
        confidence = round(min(0.88, 0.42 + 0.09 * margin + 0.03 * evidence), 2)

    drivers = sorted(
        (bullish if sentiment == "bullish" else bearish if sentiment == "bearish" else bullish + bearish),
        key=lambda item: -abs(item[1]),
    )[:3]
    driver_terms = [term for term, _ in drivers]

    rationale = (
        f"Lexicon match on {', '.join(driver_terms)} (bullish {bull_score:.1f} vs bearish {bear_score:.1f})."
        if driver_terms
        else "No directional language detected in the headline or summary."
    )

    return {
        "sentiment": sentiment,
        "confidence": confidence,
        "key_themes": detect_themes(text),
        "tickers_impacted": list(tickers or []),
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# Narrative composition
# ---------------------------------------------------------------------------
def _describe_movers(tickers: list[dict[str, Any]]) -> str:
    """Lead sentence: the best and worst performer, with real numbers."""
    ranked = [t for t in tickers if t.get("intraday_return_pct") is not None]
    if not ranked:
        return "Price data is still warming up, so no directional read is available yet."

    ranked.sort(key=lambda t: t["intraday_return_pct"], reverse=True)
    best, worst = ranked[0], ranked[-1]

    if len(ranked) == 1 or best["ticker"] == worst["ticker"]:
        return f"{best['ticker']} is at {best['price']:,.2f} ({best['intraday_return_pct']:+.2f}%)."

    if best["intraday_return_pct"] > 0 > worst["intraday_return_pct"]:
        return (
            f"{best['ticker']} leads the tape at {best['intraday_return_pct']:+.2f}% "
            f"while {worst['ticker']} lags at {worst['intraday_return_pct']:+.2f}%."
        )
    direction = "green" if best["intraday_return_pct"] > 0 else "red"
    return (
        f"The watchlist is broadly {direction}: {best['ticker']} "
        f"{best['intraday_return_pct']:+.2f}% to {worst['ticker']} {worst['intraday_return_pct']:+.2f}%."
    )


def _describe_anomalies(anomalies: list[dict[str, Any]]) -> str:
    """Second sentence: what the statistical engine actually flagged."""
    if not anomalies:
        return "The anomaly engine confirmed nothing unusual in volume, price or volatility this cycle."

    peak = max(anomalies, key=lambda a: abs(a.get("z_score", 0.0)))
    kinds = {a["anomaly_type"].replace("_", " ") for a in anomalies}
    kind_text = " and ".join(sorted(kinds))
    count = len(anomalies)
    return (
        f"{count} confirmed {kind_text} event{'s' if count != 1 else ''} cleared both the "
        f"Z-score and IQR tests, the most extreme being {peak['ticker']} at "
        f"{abs(peak['z_score']):.1f}σ."
    )


def _describe_sentiment(news: list[dict[str, Any]], fear_greed: dict[str, Any] | None) -> str:
    """Third sentence: the news tape and the market's mood gauge."""
    parts: list[str] = []

    if news:
        tally = {"bullish": 0, "bearish": 0, "neutral": 0}
        for article in news:
            tally[article.get("sentiment", "neutral")] = tally.get(article.get("sentiment", "neutral"), 0) + 1
        total = sum(tally.values())
        lead = max(tally, key=lambda key: tally[key])
        if tally[lead] == tally.get("neutral", 0) and lead != "neutral":
            lead = "mixed"
        if lead == "mixed":
            parts.append(f"News flow is evenly split across {total} recent articles")
        else:
            parts.append(f"News flow skews {lead} ({tally[lead]} of {total} recent articles)")

    if fear_greed and fear_greed.get("score") is not None:
        parts.append(
            f"CNN's Fear & Greed sits at {fear_greed['score']:.0f} ({fear_greed.get('label', 'Unknown')})"
        )

    if not parts:
        return "No news or sentiment-index readings have landed yet this session."
    return ", and ".join(parts) + "."


def classify_regime(context: dict[str, Any]) -> str:
    """Label the tape ``risk_on`` / ``risk_off`` / ``mixed`` / ``calm``."""
    returns = [
        t["intraday_return_pct"]
        for t in context.get("tickers", [])
        if t.get("intraday_return_pct") is not None
    ]
    average = sum(returns) / len(returns) if returns else 0.0
    score = (context.get("fear_greed") or {}).get("score")
    anomaly_count = len(context.get("anomalies", []))

    greedy = score is not None and score >= 55
    fearful = score is not None and score < 45

    if average > 0.3 and not fearful:
        return "risk_on"
    if average < -0.3 and not greedy:
        return "risk_off"
    if anomaly_count == 0 and abs(average) < 0.15:
        return "calm"
    return "mixed"


def compose_narrative(context: dict[str, Any]) -> dict[str, str]:
    """Write the Market Pulse briefing from measured facts alone."""
    regime = classify_regime(context)
    sentences = [
        _describe_movers(context.get("tickers", [])),
        _describe_anomalies(context.get("anomalies", [])),
        _describe_sentiment(context.get("top_news", []), context.get("fear_greed")),
    ]

    headlines = {
        "risk_on": "Risk appetite firming",
        "risk_off": "Defensive tone taking hold",
        "calm": "Quiet tape, no dislocations",
        "mixed": "Mixed signals across the tape",
    }

    return {
        "headline": headlines[regime],
        "narrative": " ".join(sentences),
        "regime": regime,
    }
