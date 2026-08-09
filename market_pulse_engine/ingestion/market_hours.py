"""US equity market-hours awareness.

Drives two behaviours:

* the price connector flags off-hours records as ``is_live = False``;
* the orchestrator skips the (pointless) price leg outside trading hours and
  ingests only news and the sentiment index.

Crypto tickers trade continuously and are never gated.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ..config import CONFIG, MARKET_CLOSE, MARKET_OPEN, MARKET_TIMEZONE
from ..utils import utcnow

#: NYSE/Nasdaq full-day closures. Half-days (1pm close) are treated as full days
#: — the cost of being wrong is one hour of `is_live=True` on three days a year.
MARKET_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2025
        date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
        date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
        date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
        # 2026
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
        # 2027
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
        date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
        date(2027, 11, 25), date(2027, 12, 24),
    }
)

OPEN_TIME = time(*MARKET_OPEN)
CLOSE_TIME = time(*MARKET_CLOSE)


def market_now(moment: datetime | None = None) -> datetime:
    """The given (or current) instant expressed in US Eastern time."""
    return (moment or utcnow()).astimezone(MARKET_TIMEZONE)


def is_trading_day(moment: datetime | None = None) -> bool:
    """True when the given instant falls on an NYSE session day."""
    eastern = market_now(moment)
    return eastern.weekday() < 5 and eastern.date() not in MARKET_HOLIDAYS


def is_market_open(moment: datetime | None = None) -> bool:
    """True during the 09:30–16:00 ET regular session on a trading day."""
    if not is_trading_day(moment):
        return False
    return OPEN_TIME <= market_now(moment).time() < CLOSE_TIME


def is_live_for(ticker: str, moment: datetime | None = None) -> bool:
    """Whether ``ticker`` is currently trading (crypto is always live)."""
    return True if CONFIG.is_crypto(ticker) else is_market_open(moment)


def session_state(moment: datetime | None = None) -> str:
    """Coarse session label: ``open`` / ``pre_market`` / ``after_hours`` / ``closed``."""
    if not is_trading_day(moment):
        return "closed"
    now_time = market_now(moment).time()
    if now_time < OPEN_TIME:
        return "pre_market"
    if now_time >= CLOSE_TIME:
        return "after_hours"
    return "open"


def next_market_open(moment: datetime | None = None) -> datetime:
    """The next regular-session open, in US Eastern time."""
    eastern = market_now(moment)
    candidate = eastern.replace(
        hour=OPEN_TIME.hour, minute=OPEN_TIME.minute, second=0, microsecond=0
    )
    if candidate <= eastern:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5 or candidate.date() in MARKET_HOLIDAYS:
        candidate += timedelta(days=1)
    return candidate
