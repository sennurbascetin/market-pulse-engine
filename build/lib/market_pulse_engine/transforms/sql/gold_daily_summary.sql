-- ---------------------------------------------------------------------------
-- Gold : per-ticker, per-session rollup
--
-- Uses arg_min/arg_max rather than a self-join to pick the session's first and
-- last print — a single pass over gold.quotes_enriched.
-- ---------------------------------------------------------------------------
INSERT OR REPLACE INTO gold.daily_summary (
    session_date, ticker, observations, first_price, last_price,
    session_high, session_low, day_return_pct, total_volume,
    avg_volatility, max_abs_volume_z, session_vwap, computed_at
)
SELECT
    session_date,
    ticker,
    count(*)                                          AS observations,
    -- The session's opening print rather than the first *close* we happened to
    -- observe, so day_return_pct is the conventional open-to-last figure.
    COALESCE(arg_min(session_open, observed_at),
             arg_min(price, observed_at))             AS first_price,
    arg_max(price, observed_at)                       AS last_price,
    max(COALESCE(day_high, price))                    AS session_high,
    min(COALESCE(day_low,  price))                    AS session_low,
    CASE
        WHEN COALESCE(arg_min(session_open, observed_at), arg_min(price, observed_at)) > 0
        THEN (arg_max(price, observed_at)
              - COALESCE(arg_min(session_open, observed_at), arg_min(price, observed_at)))
             / COALESCE(arg_min(session_open, observed_at), arg_min(price, observed_at)) * 100
    END                                               AS day_return_pct,
    CAST(sum(COALESCE(volume_delta, 0)) AS BIGINT)    AS total_volume,
    avg(volatility)                                   AS avg_volatility,
    max(abs(volume_z_score))                          AS max_abs_volume_z,
    arg_max(vwap, observed_at)                        AS session_vwap,
    now()                                             AS computed_at
FROM gold.quotes_enriched
GROUP BY session_date, ticker;
