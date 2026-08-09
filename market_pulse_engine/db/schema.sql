-- ===========================================================================
-- Market Pulse Engine — extended medallion schema
--
--   bronze    exact source payloads, append-only, never modified
--   silver    cleaned, typed, deduplicated
--   gold      analytics-ready metrics and aggregates
--   platinum  derived intelligence (anomalies, sentiment, AI narratives)
--   pipeline  operational bookkeeping
--
-- The file is idempotent: every statement is IF NOT EXISTS, so `db.init` may be
-- run against an existing database without data loss.
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS platinum;
CREATE SCHEMA IF NOT EXISTS pipeline;


-- ===========================================================================
-- BRONZE — raw landing zone
-- Payloads are stored verbatim as JSON. Only the columns needed for routing
-- and idempotency are promoted out of the payload.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS bronze.raw_quotes (
    quote_id      VARCHAR PRIMARY KEY,   -- sha256(ticker | observed_at)
    run_id        VARCHAR      NOT NULL,
    ticker        VARCHAR      NOT NULL,
    source        VARCHAR      NOT NULL DEFAULT 'yfinance',
    observed_at   TIMESTAMPTZ  NOT NULL, -- when the market data itself is stamped
    ingested_at   TIMESTAMPTZ  NOT NULL, -- when this engine captured it
    is_live       BOOLEAN      NOT NULL, -- false => market closed, last close replayed
    payload       JSON         NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.raw_news (
    article_id    VARCHAR PRIMARY KEY,   -- sha256(url)
    run_id        VARCHAR      NOT NULL,
    feed_name     VARCHAR      NOT NULL,
    feed_url      VARCHAR      NOT NULL,
    ingested_at   TIMESTAMPTZ  NOT NULL,
    payload       JSON         NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.raw_sentiment_index (
    reading_id    VARCHAR PRIMARY KEY,   -- sha256(source | observed_at)
    run_id        VARCHAR      NOT NULL,
    source        VARCHAR      NOT NULL DEFAULT 'cnn_fear_greed',
    observed_at   TIMESTAMPTZ  NOT NULL,
    ingested_at   TIMESTAMPTZ  NOT NULL,
    payload       JSON         NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bronze_quotes_ticker_ts ON bronze.raw_quotes (ticker, observed_at);
CREATE INDEX IF NOT EXISTS idx_bronze_quotes_run       ON bronze.raw_quotes (run_id);
CREATE INDEX IF NOT EXISTS idx_bronze_news_ingested    ON bronze.raw_news (ingested_at);
CREATE INDEX IF NOT EXISTS idx_bronze_sentiment_ts     ON bronze.raw_sentiment_index (observed_at);


-- ===========================================================================
-- SILVER — cleaned, typed, deduplicated
-- ===========================================================================

CREATE TABLE IF NOT EXISTS silver.quotes (
    ticker                VARCHAR      NOT NULL,
    observed_at           TIMESTAMPTZ  NOT NULL,
    price                 DOUBLE       NOT NULL,
    open                  DOUBLE,
    day_high              DOUBLE,
    day_low               DOUBLE,
    previous_close        DOUBLE,
    volume                BIGINT,
    market_cap            BIGINT,
    fifty_day_average     DOUBLE,
    two_hundred_day_average DOUBLE,
    currency              VARCHAR,
    exchange              VARCHAR,
    -- 'bar'        => volume traded during this observation (historical bars)
    -- 'cumulative' => session-to-date volume (live snapshots)
    -- Gold reconciles the two into a comparable per-observation figure.
    volume_basis          VARCHAR      NOT NULL,
    source                VARCHAR      NOT NULL,
    is_live               BOOLEAN      NOT NULL,
    ingested_at           TIMESTAMPTZ  NOT NULL,
    run_id                VARCHAR      NOT NULL,
    PRIMARY KEY (ticker, observed_at)
);

CREATE TABLE IF NOT EXISTS silver.news_articles (
    article_id        VARCHAR PRIMARY KEY,
    title             VARCHAR      NOT NULL,
    summary           VARCHAR,
    url               VARCHAR      NOT NULL,
    source            VARCHAR      NOT NULL,
    published_at      TIMESTAMPTZ,
    tickers_mentioned VARCHAR[]    NOT NULL,
    ingested_at       TIMESTAMPTZ  NOT NULL,
    run_id            VARCHAR      NOT NULL
);

CREATE TABLE IF NOT EXISTS silver.sentiment_index (
    reading_id    VARCHAR PRIMARY KEY,
    source        VARCHAR      NOT NULL,
    observed_at   TIMESTAMPTZ  NOT NULL,
    score         DOUBLE       NOT NULL,   -- 0-100 composite
    label         VARCHAR      NOT NULL,   -- "Extreme Fear" ... "Extreme Greed"
    previous_close_score DOUBLE,
    week_ago_score       DOUBLE,
    month_ago_score      DOUBLE,
    ingested_at   TIMESTAMPTZ  NOT NULL,
    run_id        VARCHAR      NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_silver_quotes_ticker_ts ON silver.quotes (ticker, observed_at);
CREATE INDEX IF NOT EXISTS idx_silver_news_published   ON silver.news_articles (published_at);


-- ===========================================================================
-- GOLD — analytics-ready metrics (populated by pure DuckDB SQL window functions)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS gold.quotes_enriched (
    ticker              VARCHAR      NOT NULL,
    observed_at         TIMESTAMPTZ  NOT NULL,
    session_date        DATE         NOT NULL,
    price               DOUBLE       NOT NULL,
    open                DOUBLE,      -- this observation's own open
    session_open        DOUBLE,      -- the session's first open, basis of intraday return
    day_high            DOUBLE,
    day_low             DOUBLE,
    volume              BIGINT,
    is_live             BOOLEAN,
    prev_price          DOUBLE,

    ma_short            DOUBLE,      -- configurable, default 5-period
    ma_mid              DOUBLE,      -- default 15-period
    ma_long             DOUBLE,      -- default 60-period

    intraday_return_pct DOUBLE,      -- (price - open) / open * 100
    period_return_pct   DOUBLE,      -- vs the previous observation
    log_return          DOUBLE,
    volatility          DOUBLE,      -- rolling stddev of log returns, annualised-free

    volume_delta        BIGINT,      -- traded volume since the previous observation
    volume_mean         DOUBLE,
    volume_stddev       DOUBLE,
    volume_z_score      DOUBLE,      -- the foundation of the anomaly engine
    price_mean          DOUBLE,
    price_stddev        DOUBLE,
    price_z_score       DOUBLE,
    volatility_mean     DOUBLE,
    volatility_stddev   DOUBLE,
    volatility_z_score  DOUBLE,

    vwap                DOUBLE,      -- volume-weighted average price, session to date
    observation_index   BIGINT,      -- 1-based position within the ticker's session
    computed_at         TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (ticker, observed_at)
);

CREATE TABLE IF NOT EXISTS gold.daily_summary (
    session_date        DATE         NOT NULL,
    ticker              VARCHAR      NOT NULL,
    observations        BIGINT       NOT NULL,
    first_price         DOUBLE,
    last_price          DOUBLE,
    session_high        DOUBLE,
    session_low         DOUBLE,
    day_return_pct      DOUBLE,
    total_volume        BIGINT,
    avg_volatility      DOUBLE,
    max_abs_volume_z    DOUBLE,
    session_vwap        DOUBLE,
    computed_at         TIMESTAMPTZ  NOT NULL,
    PRIMARY KEY (session_date, ticker)
);

CREATE INDEX IF NOT EXISTS idx_gold_enriched_ticker_ts ON gold.quotes_enriched (ticker, observed_at);


-- ===========================================================================
-- PLATINUM — derived intelligence
-- ===========================================================================

CREATE TABLE IF NOT EXISTS platinum.anomalies (
    anomaly_id     VARCHAR PRIMARY KEY,  -- sha256(ticker | observed_at | type)
    run_id         VARCHAR      NOT NULL,
    ticker         VARCHAR      NOT NULL,
    observed_at    TIMESTAMPTZ  NOT NULL,
    anomaly_type   VARCHAR      NOT NULL, -- price_spike | volume_surge | volatility_burst
    severity       VARCHAR      NOT NULL, -- low | medium | high
    direction      VARCHAR      NOT NULL, -- up | down
    metric_value   DOUBLE       NOT NULL,
    z_score        DOUBLE       NOT NULL,
    iqr_lower      DOUBLE,
    iqr_upper      DOUBLE,
    description    VARCHAR      NOT NULL,
    detected_at    TIMESTAMPTZ  NOT NULL
);

CREATE TABLE IF NOT EXISTS platinum.news_sentiment (
    article_id       VARCHAR PRIMARY KEY,
    run_id           VARCHAR      NOT NULL,
    sentiment        VARCHAR      NOT NULL,  -- bullish | bearish | neutral
    confidence       DOUBLE       NOT NULL,  -- 0.0 - 1.0
    key_themes       VARCHAR[]    NOT NULL,
    tickers_impacted VARCHAR[]    NOT NULL,
    rationale        VARCHAR,
    model            VARCHAR      NOT NULL,
    provider         VARCHAR      NOT NULL,
    scored_at        TIMESTAMPTZ  NOT NULL
);

CREATE TABLE IF NOT EXISTS platinum.market_pulse_narratives (
    narrative_id   VARCHAR PRIMARY KEY,
    run_id         VARCHAR      NOT NULL,
    generated_at   TIMESTAMPTZ  NOT NULL,
    headline       VARCHAR      NOT NULL,
    narrative      VARCHAR      NOT NULL,
    regime         VARCHAR      NOT NULL,  -- risk_on | risk_off | mixed | calm
    model          VARCHAR      NOT NULL,
    provider       VARCHAR      NOT NULL,
    tokens_used    BIGINT       NOT NULL DEFAULT 0,
    context        JSON         NOT NULL   -- exact facts the analyst was shown
);

CREATE INDEX IF NOT EXISTS idx_platinum_anomalies_ts   ON platinum.anomalies (observed_at);
CREATE INDEX IF NOT EXISTS idx_platinum_anomalies_tick ON platinum.anomalies (ticker, observed_at);
CREATE INDEX IF NOT EXISTS idx_platinum_narratives_ts  ON platinum.market_pulse_narratives (generated_at);


-- ===========================================================================
-- PIPELINE — operational bookkeeping
-- ===========================================================================

CREATE TABLE IF NOT EXISTS pipeline.run_log (
    run_id             VARCHAR PRIMARY KEY,
    started_at         TIMESTAMPTZ  NOT NULL,
    finished_at        TIMESTAMPTZ,
    duration_ms        BIGINT,
    status             VARCHAR      NOT NULL,  -- running | success | partial | failed
    mode               VARCHAR      NOT NULL,  -- live | after_hours | demo
    quotes_ingested    BIGINT       NOT NULL DEFAULT 0,
    news_ingested      BIGINT       NOT NULL DEFAULT 0,
    sentiment_ingested BIGINT       NOT NULL DEFAULT 0,
    records_ingested   BIGINT       NOT NULL DEFAULT 0,
    silver_rows        BIGINT       NOT NULL DEFAULT 0,
    gold_rows          BIGINT       NOT NULL DEFAULT 0,
    anomalies_detected BIGINT       NOT NULL DEFAULT 0,
    articles_scored    BIGINT       NOT NULL DEFAULT 0,
    llm_calls          BIGINT       NOT NULL DEFAULT 0,
    llm_tokens_used    BIGINT       NOT NULL DEFAULT 0,
    llm_provider       VARCHAR,
    error              VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_run_log_started ON pipeline.run_log (started_at);
