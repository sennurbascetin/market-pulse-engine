"""Sub-module 2B — financial news ingestion from RSS.

Five free, key-less feeds are parsed with ``feedparser``. Articles are attributed
to watchlist tickers by word-boundary matching on both the symbol and a curated
alias list (headlines say "Nvidia", almost never "NVDA").

Deduplication is by SHA-256 of the article URL, so the same story appearing in
two feeds — or on every poll for the next six hours — is stored exactly once.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import requests

from ..config import CONFIG, HTTP_HEADERS, NEWS_FEEDS
from ..logging_setup import get_logger
from ..utils import stable_id, truncate, utcnow
from .base import IngestResult, insert_bronze

log = get_logger("ingestion.news")

BRONZE_COLUMNS = ("article_id", "run_id", "feed_name", "feed_url", "ingested_at", "payload")


def _alias_patterns() -> dict[str, re.Pattern[str]]:
    """Compile one word-boundary regex per watchlist ticker."""
    patterns: dict[str, re.Pattern[str]] = {}
    for ticker in CONFIG.watchlist:
        terms = [re.escape(alias) for alias in CONFIG.aliases_for(ticker)]
        if terms:
            patterns[ticker] = re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.IGNORECASE)
    return patterns


_PATTERNS = _alias_patterns()


def extract_tickers(text: str) -> list[str]:
    """Return the watchlist tickers mentioned anywhere in ``text``."""
    if not text:
        return []
    return sorted(ticker for ticker, pattern in _PATTERNS.items() if pattern.search(text))


def _published_at(entry: Any) -> str | None:
    """Best-effort publication timestamp as an ISO-8601 UTC string."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _fetch_feed(feed: dict[str, str]) -> tuple[dict[str, str], list[Any], str | None]:
    """Download and parse a single RSS feed."""
    import feedparser

    try:
        response = requests.get(feed["url"], headers=HTTP_HEADERS, timeout=CONFIG.request_timeout)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        return feed, list(parsed.entries), None
    except Exception as exc:  # noqa: BLE001 - feeds fail in many creative ways
        return feed, [], f"{feed['name']}: {exc}"


def fetch_news(run_id: str, feeds: list[dict[str, str]] | None = None) -> IngestResult:
    """Poll every configured RSS feed and land new articles in Bronze."""
    sources = feeds if feeds is not None else NEWS_FEEDS
    result = IngestResult(source="rss_news")
    rows: list[list[Any]] = []
    seen: set[str] = set()
    now = utcnow()

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(sources)))) as pool:
        responses = list(pool.map(_fetch_feed, sources))

    for feed, entries, error in responses:
        if error:
            result.errors.append(error)
            log.warning("feed unavailable", extra={"feed": feed["name"], "error": error})
            continue

        for entry in entries:
            url = entry.get("link") or entry.get("id")
            title = (entry.get("title") or "").strip()
            if not url or not title:
                continue

            article_id = stable_id(url)
            if article_id in seen:  # the same story syndicated across two feeds
                continue
            seen.add(article_id)

            # Several feeds omit <description> entirely; the brief's fallback is
            # to carry the title through as the summary.
            summary = truncate(re.sub(r"<[^>]+>", " ", entry.get("summary") or "")) or title
            haystack = f"{title} {summary}"

            payload = {
                "title": truncate(title, 300),
                "summary": summary,
                "url": url,
                "source": feed["name"],
                "published_at": _published_at(entry),
                "tickers_mentioned": extract_tickers(haystack),
                "author": entry.get("author"),
                "tags": [tag.get("term") for tag in entry.get("tags", []) if tag.get("term")],
            }
            rows.append([article_id, run_id, feed["name"], feed["url"], now, json.dumps(payload, default=str)])

    result.fetched = len(rows)
    result.written = insert_bronze("bronze.raw_news", BRONZE_COLUMNS, rows)
    result.duplicates = result.fetched - result.written

    log.info("news ingested", extra=result.as_log_fields() | {"run_id": run_id})
    return result
