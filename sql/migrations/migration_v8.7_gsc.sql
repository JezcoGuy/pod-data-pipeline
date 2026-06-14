-- =============================================================================
-- Migration v8.7: Google Search Console ingestion
-- =============================================================================
-- Two base tables. Both daily grain.
--   1. gsc_pages_daily   per (date, page)   — organic clicks per URL
--   2. gsc_queries_daily per (date, query)  — organic clicks per search term
--
-- Populated by scripts/gsc_sync.py using OAuth credentials (NOT the GA4
-- service account — GSC rejects service account emails entirely; see
-- /opt/your_brand_id/GSC_API_Context.md). Auth uses gsc_token.pickle.
--
-- Quirks worth knowing:
--   - GSC has 2-3 day data lag. Default lookback should cover at least 14d.
--   - GSC anonymises some queries for privacy. Reported clicks/impressions
--     are < true totals (typically the gap is small but non-zero).
--   - Property `sc-domain:your_brand_id.com` verified 21 May 2026; no historical
--     data exists before then.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. gsc_pages_daily — organic clicks per URL
-- =============================================================================

CREATE TABLE IF NOT EXISTS gsc_pages_daily (
    id              BIGSERIAL       PRIMARY KEY,
    date            DATE            NOT NULL,
    brand_id        VARCHAR(64)     NOT NULL,
    page            VARCHAR(2048)   NOT NULL,
                       -- Full URL like 'https://your_brand_id.com/products/peace-sign-t-shirt'

    clicks          BIGINT          DEFAULT 0,
    impressions     BIGINT          DEFAULT 0,
    ctr             NUMERIC(8,4),
                       -- click-through rate as decimal (0.0234 = 2.34%)
    position        NUMERIC(8,4),
                       -- average search position (lower = better; 1.0 = top result)

    synced_at       TIMESTAMPTZ,

    CONSTRAINT gsc_pages_daily_unique UNIQUE (date, brand_id, page)
);

CREATE INDEX IF NOT EXISTS idx_gsc_pages_date  ON gsc_pages_daily (date, brand_id);
CREATE INDEX IF NOT EXISTS idx_gsc_pages_page  ON gsc_pages_daily (page, brand_id, date DESC);

COMMENT ON TABLE gsc_pages_daily IS
    'Daily organic search performance per URL from Google Search Console. Joinable to product_catalogue.product_handle by extracting from page URL.';


-- =============================================================================
-- 2. gsc_queries_daily — organic clicks per search term
-- =============================================================================

CREATE TABLE IF NOT EXISTS gsc_queries_daily (
    id              BIGSERIAL       PRIMARY KEY,
    date            DATE            NOT NULL,
    brand_id        VARCHAR(64)     NOT NULL,
    query           VARCHAR(2048)   NOT NULL,
                       -- The search term that produced impressions
                       -- (lowercase, as GSC reports them)

    clicks          BIGINT          DEFAULT 0,
    impressions     BIGINT          DEFAULT 0,
    ctr             NUMERIC(8,4),
    position        NUMERIC(8,4),

    synced_at       TIMESTAMPTZ,

    CONSTRAINT gsc_queries_daily_unique UNIQUE (date, brand_id, query)
);

CREATE INDEX IF NOT EXISTS idx_gsc_queries_date  ON gsc_queries_daily (date, brand_id);
CREATE INDEX IF NOT EXISTS idx_gsc_queries_query ON gsc_queries_daily (query, brand_id, date DESC);

COMMENT ON TABLE gsc_queries_daily IS
    'Daily organic search performance per query term from Google Search Console. GSC anonymises some queries for privacy.';


COMMIT;
