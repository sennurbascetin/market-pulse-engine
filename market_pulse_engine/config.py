"""Central configuration for the Market Pulse Engine.

Every setting is resolved once, at import time, from environment variables with
``MPE_``-prefixed names (see ``.env.example``). Values fall back to defaults that
let the whole engine run with no credentials and no external configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# .env loading (a deliberately tiny parser — avoids a python-dotenv dependency)
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    """Populate ``os.environ`` from a ``KEY=VALUE`` file, without overriding
    variables that are already set in the real environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Typed environment readers
# ---------------------------------------------------------------------------
def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key, "").strip()
    return value or default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return _env_str(key, str(default)).lower() in {"1", "true", "yes", "on"}


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = _env_str(key, "")
    if not raw:
        return list(default)
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def _resolve(path_value: str) -> Path:
    """Resolve a configured path relative to the project root unless absolute."""
    path = Path(path_value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


# ---------------------------------------------------------------------------
# Static reference data
# ---------------------------------------------------------------------------
DEFAULT_WATCHLIST = ["AAPL", "NVDA", "TSLA", "MSFT", "SPY", "BTC-USD", "ETH-USD"]

#: RSS sources for the news connector. Every feed is free and key-less.
NEWS_FEEDS: list[dict[str, str]] = [
    {
        "name": "Yahoo Finance",
        "url": "https://finance.yahoo.com/news/rssindex",
    },
    {
        "name": "CNBC Markets",
        "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    },
    {
        "name": "MarketWatch Top Stories",
        "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    },
    {
        "name": "Seeking Alpha Market News",
        "url": "https://seekingalpha.com/market_currents.xml",
    },
    {
        "name": "Investing.com News",
        "url": "https://www.investing.com/rss/news.rss",
    },
]

#: Company aliases used to attribute an article to a ticker when the symbol
#: itself is not written out (headlines say "Nvidia", rarely "NVDA").
TICKER_ALIASES: dict[str, list[str]] = {
    "AAPL": ["apple", "iphone", "tim cook"],
    "NVDA": ["nvidia", "jensen huang"],
    "TSLA": ["tesla", "elon musk", "cybertruck"],
    "MSFT": ["microsoft", "azure", "copilot"],
    "SPY": ["s&p 500", "s&p500", "spdr", "broad market"],
    "BTC-USD": ["bitcoin", "btc"],
    "ETH-USD": ["ethereum", "ether", "eth"],
}

#: Tickers that trade continuously, so "market hours" never gates them.
CRYPTO_SUFFIXES = ("-USD", "-USDT", "-EUR")

CNN_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

#: The CNN endpoint rejects default Python user agents.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

MARKET_TIMEZONE = ZoneInfo("America/New_York")
MARKET_OPEN = (9, 30)   # 09:30 ET
MARKET_CLOSE = (16, 0)  # 16:00 ET


# ---------------------------------------------------------------------------
# Configuration objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AnomalyConfig:
    """Thresholds for the dual-method (Z-score + IQR) anomaly engine."""

    zscore_threshold: float = field(default_factory=lambda: _env_float("MPE_ZSCORE_THRESHOLD", 2.5))
    iqr_multiplier: float = field(default_factory=lambda: _env_float("MPE_IQR_MULTIPLIER", 1.5))
    min_observations: int = field(default_factory=lambda: _env_int("MPE_MIN_OBSERVATIONS", 12))
    #: |z| boundaries mapping a confirmed anomaly onto a severity badge.
    severity_bands: tuple[tuple[float, str], ...] = (
        (4.0, "high"),
        (3.0, "medium"),
        (0.0, "low"),
    )

    def severity_for(self, z_score: float) -> str:
        """Map an absolute z-score onto ``low`` / ``medium`` / ``high``."""
        magnitude = abs(z_score)
        for floor, label in self.severity_bands:
            if magnitude >= floor:
                return label
        return "low"


@dataclass(frozen=True)
class LLMConfig:
    """Which analyst backs the intelligence layer, and how hard it is worked."""

    provider: str = field(default_factory=lambda: _env_str("MPE_LLM_PROVIDER", "auto").lower())
    openai_model: str = field(default_factory=lambda: _env_str("MPE_OPENAI_MODEL", "gpt-5-mini"))
    anthropic_model: str = field(
        default_factory=lambda: _env_str("MPE_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    )
    max_articles_per_run: int = field(default_factory=lambda: _env_int("MPE_LLM_MAX_ARTICLES", 10))
    every_n_runs: int = field(default_factory=lambda: _env_int("MPE_LLM_EVERY_N_RUNS", 5))
    request_timeout: int = field(default_factory=lambda: _env_int("MPE_LLM_TIMEOUT", 45))


@dataclass(frozen=True)
class DashboardConfig:
    host: str = field(default_factory=lambda: _env_str("MPE_DASH_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("MPE_DASH_PORT", 8050))
    refresh_seconds: int = field(default_factory=lambda: _env_int("MPE_DASH_REFRESH_SECONDS", 60))
    #: How many recent points the candlestick panel renders.
    chart_points: int = field(default_factory=lambda: _env_int("MPE_DASH_CHART_POINTS", 240))


@dataclass(frozen=True)
class Config:
    """Root configuration object. Import the module-level ``CONFIG`` singleton."""

    watchlist: list[str] = field(default_factory=lambda: _env_list("MPE_WATCHLIST", DEFAULT_WATCHLIST))
    poll_seconds: int = field(default_factory=lambda: _env_int("MPE_POLL_SECONDS", 60))
    db_path: Path = field(default_factory=lambda: _resolve(_env_str("MPE_DB_PATH", "data/market_pulse.duckdb")))
    log_path: Path = field(default_factory=lambda: _resolve(_env_str("MPE_LOG_PATH", "logs/pipeline.log")))
    demo_mode: bool = field(default_factory=lambda: _env_bool("MPE_DEMO_MODE", False))
    request_timeout: int = field(default_factory=lambda: _env_int("MPE_HTTP_TIMEOUT", 20))

    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)

    # -- rolling-window sizes used by the Gold transforms --------------------
    ma_short: int = field(default_factory=lambda: _env_int("MPE_MA_SHORT", 5))
    ma_mid: int = field(default_factory=lambda: _env_int("MPE_MA_MID", 15))
    ma_long: int = field(default_factory=lambda: _env_int("MPE_MA_LONG", 60))
    volatility_window: int = field(default_factory=lambda: _env_int("MPE_VOLATILITY_WINDOW", 20))

    def ensure_directories(self) -> None:
        """Create the data and log directories if they do not yet exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def is_crypto(self, ticker: str) -> bool:
        """True for 24/7 instruments, which are never gated by market hours."""
        return ticker.upper().endswith(CRYPTO_SUFFIXES)

    def aliases_for(self, ticker: str) -> list[str]:
        """Lower-cased match terms used to attribute news to ``ticker``."""
        base = ticker.upper()
        terms = {base.lower()}
        if "-" in base:  # BTC-USD also matches a bare "BTC"
            terms.add(base.split("-")[0].lower())
        terms.update(TICKER_ALIASES.get(base, []))
        return sorted(terms)


CONFIG = Config()
