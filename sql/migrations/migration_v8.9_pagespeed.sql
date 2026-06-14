-- =============================================================================
-- Migration v8.9: PageSpeed Insights (Lighthouse) daily snapshots
-- =============================================================================
-- One row per (date, brand_id, page_url, strategy). Populated by
-- scripts/pagespeed_sync.py via Google's PageSpeed Insights API.
--
-- Stored per category score (0-100) plus Core Web Vitals timings.
-- 'strategy' is 'mobile' or 'desktop' — Google Lighthouse audits each
-- separately and they often differ significantly.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS pagespeed_daily (
    id                      BIGSERIAL       PRIMARY KEY,
    date                    DATE            NOT NULL,
    brand_id                VARCHAR(64)     NOT NULL,
    page_url                VARCHAR(2048)   NOT NULL,
    page_path               VARCHAR(1024),
                              -- normalised path portion (e.g. '/products/peace-sign-t-shirt')
                              -- for easier joining to other tables that key on path
    strategy                VARCHAR(16)     NOT NULL
                                CHECK (strategy IN ('mobile','desktop')),

    -- Lighthouse category scores 0-100 (Google returns 0-1, we multiply)
    score_performance       INTEGER,
    score_accessibility     INTEGER,
    score_best_practices    INTEGER,
    score_seo               INTEGER,

    -- Core Web Vitals + companion metrics. Times in milliseconds.
    lcp_ms                  NUMERIC(10,2),
                              -- Largest Contentful Paint
    cls                     NUMERIC(8,4),
                              -- Cumulative Layout Shift (unitless 0-1+)
    inp_ms                  NUMERIC(10,2),
                              -- Interaction to Next Paint (replaced FID in 2024)
    fcp_ms                  NUMERIC(10,2),
                              -- First Contentful Paint
    ttfb_ms                 NUMERIC(10,2),
                              -- Time to First Byte (server response)
    tbt_ms                  NUMERIC(10,2),
                              -- Total Blocking Time
    speed_index_ms          NUMERIC(10,2),
                              -- Lighthouse Speed Index

    fetched_at              TIMESTAMPTZ,
                              -- When Lighthouse actually ran the audit
    synced_at               TIMESTAMPTZ,
                              -- When this row was upserted

    CONSTRAINT pagespeed_daily_unique UNIQUE (date, brand_id, page_url, strategy)
);

CREATE INDEX IF NOT EXISTS idx_pagespeed_date_brand
    ON pagespeed_daily (date DESC, brand_id);
CREATE INDEX IF NOT EXISTS idx_pagespeed_page
    ON pagespeed_daily (page_path, brand_id, date DESC)
    WHERE page_path IS NOT NULL;

COMMENT ON TABLE pagespeed_daily IS
    'Daily PageSpeed/Lighthouse audit snapshots. One row per (page_url, strategy) per day. Scores 0-100. Core Web Vitals in milliseconds. Source for site-perf regression alerts.';

COMMIT;
