"""Bronze-layer connectors: prices, news, and the market sentiment index."""

from .base import IngestResult
from .news_fetcher import fetch_news
from .price_fetcher import backfill_history, fetch_quotes
from .sentiment_fetcher import fetch_sentiment_index

__all__ = [
    "IngestResult",
    "backfill_history",
    "fetch_news",
    "fetch_quotes",
    "fetch_sentiment_index",
]
