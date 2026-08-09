"""Sub-module 4A — dual-method statistical anomaly detection.

Two independent detectors must agree before an event is recorded:

1. **Z-score** — flags an observation when ``|z| > threshold`` (default 2.5),
   measured against a trailing rolling mean and standard deviation.
2. **IQR / Tukey fence** — flags an observation outside
   ``[Q1 - k·IQR, Q3 + k·IQR]`` (default k = 1.5), computed over the same
   trailing window.

The z-score is sensitive but assumes roughly normal data and is itself dragged
around by the outlier it is trying to find; the IQR fence is distribution-free
and robust but blunt. Requiring **consensus** keeps the events that both a
parametric and a non-parametric view consider extreme, which is what makes the
alert feed worth reading.

Three metric families are monitored: traded volume (``volume_surge``), price
(``price_spike``) and rolling volatility (``volatility_burst``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from ..config import CONFIG
from ..db.connection import transaction
from ..logging_setup import get_logger
from ..transforms.loader import window_parameters
from ..utils import ensure_utc, stable_id, utcnow

log = get_logger("intelligence.anomaly")

#: metric key -> (anomaly_type, gold value column, gold z-score column)
METRICS: dict[str, tuple[str, str, str]] = {
    "volume": ("volume_surge", "volume_delta", "volume_z_score"),
    "price": ("price_spike", "price", "price_z_score"),
    "volatility": ("volatility_burst", "volatility", "volatility_z_score"),
}

INSERT_COLUMNS = (
    "anomaly_id", "run_id", "ticker", "observed_at", "anomaly_type", "severity",
    "direction", "metric_value", "z_score", "iqr_lower", "iqr_upper",
    "description", "detected_at",
)


@dataclass(frozen=True)
class Anomaly:
    """One confirmed anomaly, ready to be written to ``platinum.anomalies``."""

    ticker: str
    observed_at: datetime
    anomaly_type: str
    severity: str
    direction: str
    metric_value: float
    z_score: float
    iqr_lower: float | None
    iqr_upper: float | None
    description: str

    @property
    def anomaly_id(self) -> str:
        return stable_id(self.ticker, self.observed_at.isoformat(), self.anomaly_type)

    def as_row(self, run_id: str, detected_at: datetime) -> list[Any]:
        return [
            self.anomaly_id, run_id, self.ticker, self.observed_at, self.anomaly_type,
            self.severity, self.direction, self.metric_value, self.z_score,
            self.iqr_lower, self.iqr_upper, self.description, detected_at,
        ]


# ---------------------------------------------------------------------------
# Pure decision logic — no database, no config lookups, fully unit-testable
# ---------------------------------------------------------------------------
def exceeds_zscore(z_score: float | None, threshold: float) -> bool:
    """Method 1: parametric test against the rolling mean/stddev."""
    return z_score is not None and abs(z_score) > threshold


def outside_iqr_fence(
    value: float | None, q1: float | None, q3: float | None, multiplier: float
) -> bool:
    """Method 2: non-parametric Tukey fence test."""
    if value is None or q1 is None or q3 is None:
        return False
    lower, upper = iqr_bounds(q1, q3, multiplier)
    return value < lower or value > upper


def iqr_bounds(q1: float, q3: float, multiplier: float) -> tuple[float, float]:
    """Return the ``(lower, upper)`` Tukey fence for a quartile pair."""
    spread = q3 - q1
    return q1 - multiplier * spread, q3 + multiplier * spread


def is_consensus_anomaly(
    value: float | None,
    z_score: float | None,
    q1: float | None,
    q3: float | None,
    *,
    zscore_threshold: float,
    iqr_multiplier: float,
) -> bool:
    """The consensus rule: an anomaly requires *both* methods to agree.

    A zero-width IQR — a perfectly flat trailing window, which happens for an
    untraded ticker or a halted symbol — would fence out *every* value that
    differs at all, turning the robust test into a hair trigger. Such windows
    are rejected outright.

    The trade-off is deliberate and worth knowing: against a perfectly constant
    baseline this engine confirms nothing, no matter how large the excursion.
    Real instruments always carry tick-to-tick variation, so the fence has
    width in practice; a series flat to the cent is a data-feed artefact, and
    staying silent on it is the correct behaviour.
    """
    if q1 is not None and q3 is not None and q3 - q1 <= 0:
        return False
    return exceeds_zscore(z_score, zscore_threshold) and outside_iqr_fence(
        value, q1, q3, iqr_multiplier
    )


def _format_number(value: float) -> str:
    """Human-scale a magnitude: 5_932_233 -> ``5.9M``."""
    magnitude = abs(value)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= divisor:
            return f"{value / divisor:,.1f}{suffix}"
    return f"{value:,.2f}" if magnitude < 1000 else f"{value:,.0f}"


def describe(
    ticker: str, anomaly_type: str, value: float, z_score: float, direction: str
) -> str:
    """One-line, human-readable explanation used by the dashboard alert feed."""
    sigma = f"{abs(z_score):.1f}σ"
    if anomaly_type == "volume_surge":
        # Deliberately unit-free: equities report share counts, crypto pairs
        # report notional, and the feed mixes both.
        return (
            f"{ticker} volume hit {_format_number(value)} in one interval — "
            f"{sigma} {'above' if direction == 'up' else 'below'} its recent average."
        )
    if anomaly_type == "price_spike":
        return (
            f"{ticker} printed {_format_number(value)}, {sigma} "
            f"{'above' if direction == 'up' else 'below'} its rolling mean price."
        )
    return (
        f"{ticker} realised volatility jumped to {value:.4f} — {sigma} "
        f"{'above' if direction == 'up' else 'below'} its recent baseline."
    )


# ---------------------------------------------------------------------------
# Candidate extraction — rolling quartiles computed by DuckDB, not NumPy loops
# ---------------------------------------------------------------------------
def _candidate_sql(lookback_hours: int | None) -> tuple[str, list[Any]]:
    stat_lag = window_parameters()["stat_lag"]
    params: list[Any] = []

    scope_filter = ""
    if lookback_hours is not None:
        scope_filter = "WHERE observed_at >= ?"
        params.append(utcnow() - timedelta(hours=lookback_hours))

    quartiles = ",\n            ".join(
        f"CAST(quantile_cont({column}, 0.25) OVER w AS DOUBLE) AS {key}_q1,\n            "
        f"CAST(quantile_cont({column}, 0.75) OVER w AS DOUBLE) AS {key}_q3"
        for key, (_, column, _) in METRICS.items()
    )
    z_filter = " OR ".join(f"abs({z}) > ?" for _, _, z in METRICS.values())
    params.extend([CONFIG.anomaly.zscore_threshold] * len(METRICS))
    params.append(CONFIG.anomaly.min_observations)

    sql = f"""
        WITH scoped AS (
            SELECT ticker, observed_at, price, volume_delta, volatility,
                   volume_z_score, price_z_score, volatility_z_score,
                   count(*) OVER (PARTITION BY ticker) AS ticker_observations
            FROM gold.quotes_enriched
            {scope_filter}
        ),
        bounds AS (
            SELECT *,
            {quartiles}
            FROM scoped
            WINDOW w AS (
                PARTITION BY ticker ORDER BY observed_at
                ROWS BETWEEN {stat_lag} PRECEDING AND CURRENT ROW
            )
        )
        SELECT * FROM bounds
        WHERE ({z_filter})
          AND ticker_observations >= ?
        ORDER BY observed_at
    """
    return sql, params


def find_anomalies(lookback_hours: int | None = None) -> list[Anomaly]:
    """Evaluate the consensus rule over Gold and return confirmed anomalies."""
    from ..db.connection import get_connection

    sql, params = _candidate_sql(lookback_hours)
    rows = get_connection().execute(sql, params).fetchdf().to_dict("records")
    return list(_evaluate(rows))


def _evaluate(rows: Iterable[dict[str, Any]]) -> Iterable[Anomaly]:
    """Apply the consensus rule to each candidate row and metric family."""
    settings = CONFIG.anomaly
    for row in rows:
        for key, (anomaly_type, value_column, z_column) in METRICS.items():
            value = _clean(row.get(value_column))
            z_score = _clean(row.get(z_column))
            q1 = _clean(row.get(f"{key}_q1"))
            q3 = _clean(row.get(f"{key}_q3"))

            if not is_consensus_anomaly(
                value,
                z_score,
                q1,
                q3,
                zscore_threshold=settings.zscore_threshold,
                iqr_multiplier=settings.iqr_multiplier,
            ):
                continue

            assert value is not None and z_score is not None  # guaranteed above
            lower, upper = iqr_bounds(q1, q3, settings.iqr_multiplier)  # type: ignore[arg-type]
            direction = "up" if z_score > 0 else "down"
            ticker = str(row["ticker"])

            yield Anomaly(
                ticker=ticker,
                observed_at=ensure_utc(row["observed_at"]),
                anomaly_type=anomaly_type,
                severity=settings.severity_for(z_score),
                direction=direction,
                metric_value=float(value),
                z_score=float(z_score),
                iqr_lower=lower,
                iqr_upper=upper,
                description=describe(ticker, anomaly_type, float(value), float(z_score), direction),
            )


def _clean(value: Any) -> float | None:
    """Coerce a DataFrame cell to float, mapping NaN/NaT/None onto ``None``."""
    from ..utils import to_float

    return to_float(value)


@dataclass
class DetectionResult:
    """Outcome of one scan.

    ``confirmed`` counts every event in the scanned window; ``newly_recorded``
    counts only those not already in ``platinum.anomalies``. The run log wants
    the latter — otherwise every cycle would re-report the same backlog and the
    session totals would be meaningless.
    """

    anomalies: list[Anomaly]
    newly_recorded: int = 0

    @property
    def confirmed(self) -> int:
        return len(self.anomalies)

    def __len__(self) -> int:  # so callers can still do len(result)
        return len(self.anomalies)

    def __iter__(self):
        return iter(self.anomalies)


def detect(run_id: str, lookback_hours: int | None = None) -> DetectionResult:
    """Detect anomalies and persist them to ``platinum.anomalies``.

    Anomaly ids are deterministic, so re-running over the same window inserts
    nothing rather than producing duplicate alerts.
    """
    anomalies = find_anomalies(lookback_hours)
    detected_at = utcnow()
    newly_recorded = 0

    if anomalies:
        statement = (
            f"INSERT INTO platinum.anomalies ({', '.join(INSERT_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(INSERT_COLUMNS))}) ON CONFLICT DO NOTHING"
        )
        with transaction() as conn:
            before = conn.execute("SELECT count(*) FROM platinum.anomalies").fetchone()[0]
            conn.executemany(statement, [a.as_row(run_id, detected_at) for a in anomalies])
            after = conn.execute("SELECT count(*) FROM platinum.anomalies").fetchone()[0]
        newly_recorded = after - before

    by_severity: dict[str, int] = {}
    for anomaly in anomalies:
        by_severity[anomaly.severity] = by_severity.get(anomaly.severity, 0) + 1

    log.info(
        "anomaly scan complete",
        extra={
            "run_id": run_id,
            "confirmed": len(anomalies),
            "new": newly_recorded,
            "lookback_hours": lookback_hours,
            "zscore_threshold": CONFIG.anomaly.zscore_threshold,
            "iqr_multiplier": CONFIG.anomaly.iqr_multiplier,
            **{f"severity_{k}": v for k, v in by_severity.items()},
        },
    )
    return DetectionResult(anomalies=anomalies, newly_recorded=newly_recorded)
