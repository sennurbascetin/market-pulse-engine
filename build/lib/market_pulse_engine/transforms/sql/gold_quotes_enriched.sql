-- ---------------------------------------------------------------------------
-- Silver -> Gold : per-observation analytics
--
-- One chained query. Every metric is a DuckDB window function; there is not a
-- single Pandas loop in the transform path. Window sizes are injected from
-- config.py ({ma_short}/{ma_mid}/{ma_long}/{vol_window}/{stat_window} periods).
--
-- CTE chain
--   base        session-stamped observations
--   reconciled  bar volume and cumulative volume unified into volume_delta
--   returns     previous price + intraday return
--   derived     period return + log return
--   rolled      moving averages, volatility, VWAP, session position
--   scored      rolling mean/stddev feeding the z-scores
-- ---------------------------------------------------------------------------
INSERT OR REPLACE INTO gold.quotes_enriched (
    ticker, observed_at, session_date, price, open, session_open, day_high, day_low, volume,
    is_live, prev_price, ma_short, ma_mid, ma_long,
    intraday_return_pct, period_return_pct, log_return, volatility,
    volume_delta, volume_mean, volume_stddev, volume_z_score,
    price_mean, price_stddev, price_z_score,
    volatility_mean, volatility_stddev, volatility_z_score,
    vwap, observation_index, computed_at
)
WITH base AS (
    SELECT
        ticker,
        observed_at,
        price,
        open,
        day_high,
        day_low,
        volume,
        volume_basis,
        is_live,
        -- Sessions are defined in exchange-local time so a 20:00 UTC print and
        -- a 13:30 UTC print on the same US trading day group together.
        CAST(observed_at AT TIME ZONE 'America/New_York' AS DATE) AS session_date
    FROM silver.quotes
),
reconciled AS (
    SELECT
        *,
        -- Historical bars already carry per-interval volume. Live snapshots
        -- carry session-to-date volume, so the traded amount is the increment
        -- over the previous *cumulative* reading in the same session.
        --
        -- Two deliberate edges:
        --   * the session's FIRST cumulative reading differences against itself
        --     and so contributes 0. Its volume accrued before the engine was
        --     watching; booking it as one interval's trade would manufacture a
        --     huge false volume surge whenever the pipeline starts mid-session.
        --   * GREATEST(..., 0) clamps a counter reset so a new session can
        --     never produce a negative delta.
        CASE
            WHEN volume_basis = 'bar' THEN volume
            ELSE GREATEST(
                volume - COALESCE(
                    last_value(CASE WHEN volume_basis = 'cumulative' THEN volume END IGNORE NULLS)
                        OVER (
                            PARTITION BY ticker, session_date
                            ORDER BY observed_at
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                        ),
                    volume
                ),
                0
            )
        END AS volume_delta
    FROM base
),
returns AS (
    SELECT
        *,
        LAG(price) OVER (PARTITION BY ticker ORDER BY observed_at) AS prev_price,
        -- The session's opening print, not this observation's own open: a 5-minute
        -- historical bar carries the bar's open, which would otherwise turn the
        -- intraday return into a five-minute return.
        first_value(open) OVER (
            PARTITION BY ticker, session_date ORDER BY observed_at) AS session_open
    FROM reconciled
),
derived AS (
    SELECT
        *,
        CASE WHEN session_open > 0
             THEN (price - session_open) / session_open * 100 END                 AS intraday_return_pct,
        CASE WHEN prev_price > 0 THEN (price - prev_price) / prev_price * 100 END AS period_return_pct,
        CASE WHEN prev_price > 0 AND price > 0 THEN ln(price / prev_price) END    AS log_return
    FROM returns
),
rolled AS (
    SELECT
        *,
        avg(price) OVER (
            PARTITION BY ticker ORDER BY observed_at
            ROWS BETWEEN {ma_short_lag} PRECEDING AND CURRENT ROW) AS ma_short,
        avg(price) OVER (
            PARTITION BY ticker ORDER BY observed_at
            ROWS BETWEEN {ma_mid_lag} PRECEDING AND CURRENT ROW)   AS ma_mid,
        avg(price) OVER (
            PARTITION BY ticker ORDER BY observed_at
            ROWS BETWEEN {ma_long_lag} PRECEDING AND CURRENT ROW)  AS ma_long,
        stddev_samp(log_return) OVER (
            PARTITION BY ticker ORDER BY observed_at
            ROWS BETWEEN {vol_lag} PRECEDING AND CURRENT ROW)      AS volatility,
        -- Session-to-date VWAP: cumulative notional over cumulative volume.
        sum(price * volume_delta) OVER (
            PARTITION BY ticker, session_date ORDER BY observed_at
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        / NULLIF(sum(volume_delta) OVER (
            PARTITION BY ticker, session_date ORDER BY observed_at
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 0)  AS vwap,
        row_number() OVER (
            PARTITION BY ticker, session_date ORDER BY observed_at) AS observation_index
    FROM derived
),
scored AS (
    SELECT
        *,
        avg(volume_delta)          OVER stat AS volume_mean,
        stddev_samp(volume_delta)  OVER stat AS volume_stddev,
        avg(price)                 OVER stat AS price_mean,
        stddev_samp(price)         OVER stat AS price_stddev,
        avg(volatility)            OVER stat AS volatility_mean,
        stddev_samp(volatility)    OVER stat AS volatility_stddev
    FROM rolled
    WINDOW stat AS (
        PARTITION BY ticker ORDER BY observed_at
        ROWS BETWEEN {stat_lag} PRECEDING AND CURRENT ROW
    )
)
SELECT
    ticker,
    observed_at,
    session_date,
    price,
    open,
    session_open,
    day_high,
    day_low,
    volume,
    is_live,
    prev_price,
    ma_short,
    ma_mid,
    ma_long,
    intraday_return_pct,
    period_return_pct,
    log_return,
    volatility,
    volume_delta,
    volume_mean,
    volume_stddev,
    CASE WHEN volume_stddev > 0
         THEN (volume_delta - volume_mean) / volume_stddev END     AS volume_z_score,
    price_mean,
    price_stddev,
    CASE WHEN price_stddev > 0
         THEN (price - price_mean) / price_stddev END              AS price_z_score,
    volatility_mean,
    volatility_stddev,
    CASE WHEN volatility_stddev > 0
         THEN (volatility - volatility_mean) / volatility_stddev END AS volatility_z_score,
    vwap,
    observation_index,
    now()                                                          AS computed_at
FROM scored;
