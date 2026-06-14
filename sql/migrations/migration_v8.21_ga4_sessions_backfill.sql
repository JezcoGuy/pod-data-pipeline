-- Migration v8.21 — GA4 sessions / funnel backfill
-- --------------------------------------------------
-- Live GA4 sync (ga4_channels_daily) only covers 2026-05-09 onward. Pre-GA4
-- session metrics were tracked manually in a Shopify Analytics spreadsheet
-- (Jul 2025 → May 8 2026). This table holds that backfill, marked with
-- data_source='shopify_analytics' so downstream consumers can distinguish
-- the two sources via v_sessions_daily.
--
-- View v_sessions_daily (defined in a follow-up migration after ingest)
-- unions this table with ga4_channels_daily so dashboards get a continuous
-- session/funnel history from Jul 2025 to today.

CREATE TABLE IF NOT EXISTS ga4_sessions_backfill (
    id                    SERIAL       PRIMARY KEY,
    date                  DATE         NOT NULL,
    brand_id              VARCHAR(64)  NOT NULL DEFAULT 'your_brand_id',
    data_source           VARCHAR(32)  DEFAULT 'shopify_analytics',  -- 'shopify_analytics' for the backfill rows
    sessions              INTEGER,
    atc                   INTEGER,
    atc_rate_pct          NUMERIC(6,2),
    reached_checkout      INTEGER,
    reached_checkout_pct  NUMERIC(6,2),
    purchases             INTEGER,
    cr_pct                NUMERIC(6,2),
    returning_orders      INTEGER,
    returning_pct         NUMERIC(6,2),
    designs_uploaded      INTEGER,
    ads_launched          INTEGER,
    emails_sent           INTEGER,
    synced_at             TIMESTAMPTZ  DEFAULT NOW(),
    UNIQUE (date, brand_id)
);

CREATE INDEX IF NOT EXISTS idx_ga4_backfill_date
    ON ga4_sessions_backfill (date DESC);
