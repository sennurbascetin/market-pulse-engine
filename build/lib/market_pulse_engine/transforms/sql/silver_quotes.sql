-- ---------------------------------------------------------------------------
-- Bronze -> Silver : quotes
--
-- Parses the raw yfinance payloads, enforces strict types, applies the null
-- fill strategy, and deduplicates to one row per (ticker, observed_at).
--
-- When a live snapshot and a historical bar describe the same instant the
-- snapshot wins: it is the higher-fidelity observation.
-- ---------------------------------------------------------------------------
INSERT OR REPLACE INTO silver.quotes (
    ticker, observed_at, price, open, day_high, day_low, previous_close,
    volume, market_cap, fifty_day_average, two_hundred_day_average,
    currency, exchange, volume_basis, source, is_live, ingested_at, run_id
)
WITH parsed AS (
    SELECT
        ticker,
        observed_at,
        ingested_at,
        run_id,
        source,
        is_live,
        COALESCE(
            TRY_CAST(payload ->> 'currentPrice'       AS DOUBLE),
            TRY_CAST(payload ->> 'regularMarketPrice' AS DOUBLE)
        )                                                          AS price,
        TRY_CAST(payload ->> 'open'                AS DOUBLE)      AS open,
        TRY_CAST(payload ->> 'dayHigh'             AS DOUBLE)      AS day_high,
        TRY_CAST(payload ->> 'dayLow'              AS DOUBLE)      AS day_low,
        TRY_CAST(payload ->> 'previousClose'       AS DOUBLE)      AS previous_close,
        TRY_CAST(TRY_CAST(COALESCE(payload ->> 'volume',
                                   payload ->> 'regularMarketVolume') AS DOUBLE) AS BIGINT) AS volume,
        TRY_CAST(TRY_CAST(payload ->> 'marketCap'  AS DOUBLE) AS BIGINT)         AS market_cap,
        TRY_CAST(payload ->> 'fiftyDayAverage'        AS DOUBLE)   AS fifty_day_average,
        TRY_CAST(payload ->> 'twoHundredDayAverage'   AS DOUBLE)   AS two_hundred_day_average,
        payload ->> 'currency'                                     AS currency,
        payload ->> 'exchange'                                     AS exchange,
        -- Historical bars report the volume traded *within* the bar; live
        -- snapshots report session-to-date volume. Gold needs to know which.
        CASE WHEN source = 'yfinance_history' THEN 'bar' ELSE 'cumulative' END AS volume_basis
    FROM bronze.raw_quotes
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY ticker, observed_at
            ORDER BY (source = 'yfinance') DESC, ingested_at DESC
        ) AS revision
    FROM parsed
    WHERE price IS NOT NULL AND price > 0
)
SELECT
    ticker,
    observed_at,
    price,
    -- Null fill strategy: fall back to the tightest defensible substitute
    -- rather than dropping the observation entirely.
    COALESCE(open, price)                        AS open,
    COALESCE(day_high, GREATEST(price, COALESCE(open, price))) AS day_high,
    COALESCE(day_low,  LEAST(price,    COALESCE(open, price))) AS day_low,
    previous_close,
    COALESCE(volume, 0)                          AS volume,
    market_cap,
    fifty_day_average,
    two_hundred_day_average,
    COALESCE(currency, 'USD')                    AS currency,
    exchange,
    volume_basis,
    source,
    is_live,
    ingested_at,
    run_id
FROM ranked
WHERE revision = 1;
