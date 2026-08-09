-- ---------------------------------------------------------------------------
-- Bronze -> Silver : news articles
--
-- Bronze already deduplicates by URL hash, so the only work here is typing the
-- payload, guaranteeing a non-null title/summary, and lifting the ticker
-- attribution array into a native DuckDB VARCHAR[].
-- ---------------------------------------------------------------------------
INSERT OR REPLACE INTO silver.news_articles (
    article_id, title, summary, url, source, published_at,
    tickers_mentioned, ingested_at, run_id
)
SELECT
    article_id,
    trim(payload ->> 'title')                                    AS title,
    -- Feeds such as Yahoo and Seeking Alpha omit <description>; carrying the
    -- title through keeps every article scoreable by the intelligence layer.
    COALESCE(NULLIF(trim(payload ->> 'summary'), ''),
             trim(payload ->> 'title'))                          AS summary,
    payload ->> 'url'                                            AS url,
    COALESCE(payload ->> 'source', feed_name)                    AS source,
    TRY_CAST(payload ->> 'published_at' AS TIMESTAMPTZ)          AS published_at,
    COALESCE(TRY_CAST(payload -> 'tickers_mentioned' AS VARCHAR[]), []) AS tickers_mentioned,
    ingested_at,
    run_id
FROM bronze.raw_news
WHERE NULLIF(trim(COALESCE(payload ->> 'title', '')), '') IS NOT NULL
  AND NULLIF(trim(COALESCE(payload ->> 'url',   '')), '') IS NOT NULL;
