"""Small shared helpers: deterministic ids, timestamps, safe coercion."""

from __future__ import annotations

import hashlib
import math
import uuid
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """Timezone-aware UTC now — the engine never handles naive datetimes."""
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    """A short, sortable-ish identifier for one pipeline cycle."""
    return f"run_{utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"


def stable_id(*parts: Any) -> str:
    """Deterministic 32-char id from the given parts.

    Used as the natural primary key throughout Bronze, which makes every
    ingestion write idempotent: re-fetching the same observation produces the
    same id and is discarded by ``ON CONFLICT DO NOTHING``.
    """
    joined = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def to_float(value: Any) -> float | None:
    """Coerce to ``float``, mapping NaN/inf/None/'' onto ``None``."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def to_int(value: Any) -> int | None:
    """Coerce to ``int``, tolerating floats and rejecting NaN/inf."""
    number = to_float(value)
    return None if number is None else int(number)


def epoch_to_utc(value: Any) -> datetime | None:
    """Convert a Unix epoch (seconds) into an aware UTC datetime."""
    seconds = to_float(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def ensure_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive datetime; convert an aware one to UTC.

    Missing values from a DataFrame arrive as pandas ``NaT``, which is a
    datetime-like object rather than ``None`` and would otherwise propagate a
    NaN all the way into arithmetic. Like NaN, it compares unequal to itself —
    that is the check used here, so this module stays pandas-free.
    """
    if value is None or value != value:  # noqa: PLR0124 - NaN/NaT self-comparison
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def truncate(text: str | None, limit: int = 600) -> str | None:
    """Trim free text to ``limit`` characters on a word boundary."""
    if not text:
        return None
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit(" ", 1)[0] + "…"


def humanise_age(moment: datetime | None, *, now: datetime | None = None) -> str:
    """Render a timestamp as ``"12 seconds ago"`` / ``"3 minutes ago"``."""
    moment = ensure_utc(moment)
    if moment is None:
        return "never"
    reference = now or utcnow()
    seconds = int((reference - moment).total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''} ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"
