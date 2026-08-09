"""Phase 4A — the dual-method anomaly engine.

Two layers are tested separately:

* the **decision logic** as pure functions, with no database in the picture;
* the **end-to-end scan**, by injecting a known spike into an otherwise
  well-behaved series and asserting it is the only thing flagged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from market_pulse_engine.config import CONFIG
from market_pulse_engine.intelligence import anomaly_detector as detector
from market_pulse_engine.transforms import gold, silver

from helpers import insert_raw_quote, seed_series

START = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)


def _build():
    silver.run_all()
    gold.run_all()


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("z_score", "threshold", "expected"),
    [(3.0, 2.5, True), (2.5, 2.5, False), (-3.0, 2.5, True), (None, 2.5, False), (0.0, 2.5, False)],
)
def test_zscore_method(z_score, threshold, expected):
    assert detector.exceeds_zscore(z_score, threshold) is expected


def test_iqr_bounds():
    lower, upper = detector.iqr_bounds(10.0, 20.0, 1.5)
    assert lower == pytest.approx(-5.0)   # 10 - 1.5*10
    assert upper == pytest.approx(35.0)   # 20 + 1.5*10


@pytest.mark.parametrize(
    ("value", "expected"),
    [(40.0, True), (-10.0, True), (35.0, False), (15.0, False), (None, False)],
)
def test_iqr_method(value, expected):
    assert detector.outside_iqr_fence(value, 10.0, 20.0, 1.5) is expected


def test_consensus_requires_both_methods():
    """The whole point of the rule: either method alone is not enough."""
    kwargs = {"zscore_threshold": 2.5, "iqr_multiplier": 1.5}

    # both agree -> anomaly
    assert detector.is_consensus_anomaly(100.0, 4.0, 10.0, 20.0, **kwargs) is True
    # z-score fires, IQR does not -> suppressed
    assert detector.is_consensus_anomaly(25.0, 4.0, 10.0, 20.0, **kwargs) is False
    # IQR fires, z-score does not -> suppressed
    assert detector.is_consensus_anomaly(100.0, 1.0, 10.0, 20.0, **kwargs) is False
    # neither -> suppressed
    assert detector.is_consensus_anomaly(15.0, 0.5, 10.0, 20.0, **kwargs) is False


def test_degenerate_iqr_is_rejected():
    """A flat window has a zero-width fence and would flag every distinct value."""
    assert detector.is_consensus_anomaly(
        999.0, 9.0, 10.0, 10.0, zscore_threshold=2.5, iqr_multiplier=1.5
    ) is False


@pytest.mark.parametrize(
    ("z_score", "expected"),
    [(2.6, "low"), (-2.9, "low"), (3.0, "medium"), (3.9, "medium"), (4.0, "high"), (-7.5, "high")],
)
def test_severity_bands(z_score, expected):
    assert CONFIG.anomaly.severity_for(z_score) == expected


def test_descriptions_are_unit_neutral_and_directional():
    up = detector.describe("NVDA", "volume_surge", 5_932_233, 6.7, "up")
    assert "5.9M" in up and "6.7σ" in up and "above" in up
    # Crypto reports notional rather than share counts, so no "shares" wording.
    assert "shares" not in up

    down = detector.describe("TSLA", "price_spike", 328.58, -3.1, "down")
    assert "below" in down and "3.1σ" in down


# ---------------------------------------------------------------------------
# End-to-end detection
# ---------------------------------------------------------------------------
def _volumes_with_spike(count: int, spike_at: int, spike: int) -> list[int]:
    """Mildly varying volume (so stddev > 0) with one large injected spike."""
    volumes = [1_000 + (index % 5) * 10 for index in range(count)]
    volumes[spike_at] = spike
    return volumes


def test_injected_volume_spike_is_detected(db, run_id):
    count, spike_at = 80, 60
    seed_series(
        db, "SPIKE", count=count, start_price=100.0, step=0.0,
        volumes=_volumes_with_spike(count, spike_at, 5_000),
    )
    _build()

    result = detector.detect(run_id)
    surges = [a for a in result.anomalies if a.anomaly_type == "volume_surge"]

    assert len(surges) == 1
    found = surges[0]
    assert found.ticker == "SPIKE"
    assert found.observed_at == START + timedelta(minutes=5 * spike_at)
    assert found.direction == "up"
    assert found.severity == "high"
    assert found.z_score > CONFIG.anomaly.zscore_threshold
    assert found.metric_value == pytest.approx(5_000)


def test_well_behaved_series_produces_no_anomalies(db, run_id):
    """The false-positive guard: ordinary noise must stay quiet."""
    count = 80
    volumes = [1_000 + (index % 5) * 10 for index in range(count)]
    seed_series(db, "CALM", count=count, start_price=100.0, step=0.0, volumes=volumes)
    _build()

    assert detector.detect(run_id).anomalies == []


def test_detected_anomalies_are_persisted_with_full_detail(db, run_id):
    count, spike_at = 80, 60
    seed_series(
        db, "SPIKE", count=count, start_price=100.0, step=0.0,
        volumes=_volumes_with_spike(count, spike_at, 5_000),
    )
    _build()
    detector.detect(run_id)

    row = db.execute(
        """
        SELECT anomaly_id, run_id, ticker, anomaly_type, severity, direction,
               metric_value, z_score, iqr_lower, iqr_upper, description, detected_at
        FROM platinum.anomalies WHERE anomaly_type = 'volume_surge'
        """
    ).fetchone()

    assert row is not None
    (anomaly_id, stored_run, ticker, kind, severity, direction,
     value, z_score, iqr_lower, iqr_upper, description, detected_at) = row

    assert len(anomaly_id) == 32
    assert stored_run == run_id
    assert ticker == "SPIKE"
    assert kind == "volume_surge"
    assert severity in {"low", "medium", "high"}
    assert direction == "up"
    assert value == pytest.approx(5_000)
    assert abs(z_score) > CONFIG.anomaly.zscore_threshold
    assert iqr_upper > iqr_lower
    assert "SPIKE" in description
    assert detected_at.tzinfo is not None


def test_rescanning_records_nothing_new(db, run_id):
    count, spike_at = 80, 60
    seed_series(
        db, "SPIKE", count=count, start_price=100.0, step=0.0,
        volumes=_volumes_with_spike(count, spike_at, 5_000),
    )
    _build()

    first = detector.detect(run_id)
    second = detector.detect("run_test_000002")

    assert first.newly_recorded == first.confirmed > 0
    assert second.confirmed == first.confirmed      # same events still confirmed
    assert second.newly_recorded == 0               # but nothing new is written
    assert db.execute("SELECT count(*) FROM platinum.anomalies").fetchone()[0] == first.confirmed


def test_short_series_is_ignored(db, run_id):
    """Below min_observations there is not enough history to trust a z-score."""
    volumes = [1_000, 1_010, 1_020, 50_000]
    seed_series(db, "TINY", count=4, start_price=100.0, step=0.0, volumes=volumes)
    _build()

    assert detector.detect(run_id).anomalies == []


def test_price_spike_is_classified_separately_from_volume(db, run_id):
    """A price outlier is reported as price_spike, and volume stays quiet.

    The baseline carries small tick-to-tick variation on purpose: a perfectly
    constant series has a zero-width IQR fence, which the consensus rule
    deliberately refuses to act on (see the degenerate-fence test above).
    """
    count, spike_at = 80, 60
    prices = [100.0 + (index % 5) * 0.1 for index in range(count)]
    prices[spike_at] = 140.0

    for index, price in enumerate(prices):
        insert_raw_quote(
            db, "PX", START + timedelta(minutes=5 * index),
            price=price, volume=1_000 + (index % 5) * 10, open_price=100.0,
        )
    _build()

    anomalies = detector.detect(run_id).anomalies
    kinds = {a.anomaly_type for a in anomalies}
    assert "price_spike" in kinds
    assert "volume_surge" not in kinds

    spike = next(a for a in anomalies if a.anomaly_type == "price_spike")
    assert spike.metric_value == pytest.approx(140.0)
    assert spike.direction == "up"
