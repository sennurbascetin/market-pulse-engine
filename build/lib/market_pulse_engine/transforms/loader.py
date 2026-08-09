"""Loads the transform SQL from disk and renders configurable window sizes.

Keeping the SQL in ``.sql`` files rather than Python string literals means the
transform logic is readable in isolation, diffable, and can be pasted straight
into a DuckDB shell for debugging.
"""

from __future__ import annotations

from pathlib import Path

from ..config import CONFIG

SQL_DIR = Path(__file__).with_name("sql")


def window_parameters() -> dict[str, int]:
    """Frame offsets for the Gold windows.

    A DuckDB frame of ``ROWS BETWEEN n PRECEDING AND CURRENT ROW`` spans ``n+1``
    rows, so an N-period window needs an offset of ``N-1``. Frame bounds cannot
    be bound parameters, hence rendering them into the SQL text — the values are
    integers straight from config and are cast defensively here.
    """
    return {
        "ma_short_lag": max(int(CONFIG.ma_short) - 1, 0),
        "ma_mid_lag": max(int(CONFIG.ma_mid) - 1, 0),
        "ma_long_lag": max(int(CONFIG.ma_long) - 1, 0),
        "vol_lag": max(int(CONFIG.volatility_window) - 1, 0),
        "stat_lag": max(int(CONFIG.ma_long) - 1, 0),
    }


def load_sql(name: str, **overrides: int) -> str:
    """Return the named SQL file with window parameters substituted.

    Substitution replaces only the known ``{placeholder}`` keys rather than
    using ``str.format``, so braces elsewhere in the SQL — JSON literals, or a
    comment mentioning ``{ma_short}`` — pass through untouched.
    """
    text = (SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")
    for key, value in (window_parameters() | overrides).items():
        text = text.replace("{" + key + "}", str(int(value)))
    return text
