-- ---------------------------------------------------------------------------
-- Bronze -> Silver : market sentiment index (CNN Fear & Greed)
-- ---------------------------------------------------------------------------
INSERT OR REPLACE INTO silver.sentiment_index (
    reading_id, source, observed_at, score, label,
    previous_close_score, week_ago_score, month_ago_score, ingested_at, run_id
)
SELECT
    reading_id,
    source,
    observed_at,
    TRY_CAST(payload ->> 'score' AS DOUBLE)             AS score,
    COALESCE(NULLIF(payload ->> 'rating', ''), 'Unknown') AS label,
    TRY_CAST(payload ->> 'previous_close'   AS DOUBLE)  AS previous_close_score,
    TRY_CAST(payload ->> 'previous_1_week'  AS DOUBLE)  AS week_ago_score,
    TRY_CAST(payload ->> 'previous_1_month' AS DOUBLE)  AS month_ago_score,
    ingested_at,
    run_id
FROM bronze.raw_sentiment_index
WHERE TRY_CAST(payload ->> 'score' AS DOUBLE) IS NOT NULL;
