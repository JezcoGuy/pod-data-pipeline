-- =============================================================================
-- Migration v8.4: GA4 ingestion + funnel view
-- =============================================================================
-- Three base tables and one derived view:
--
--   1. ga4_channels_daily   -- per (date, channel, source, medium, campaign)
--                              Top-level marketing channel mix + UTM detail
--   2. ga4_products_daily   -- per (date, item_id, channel)
--                              Per-product channel attribution
--                              Solves the "is this product's revenue from Meta
--                              or organic?" question
--   3. ga4_pages_daily      -- per (date, page_path)
--                              Landing page + page-level engagement
--   4. v_ga4_funnel_daily   -- view, per (date, channel)
--                              Aggregated funnel rates derived from
--                              ga4_channels_daily; no separate base table
--
-- Populated by scripts/ga4_sync.py via Google Analytics Data API.
--
-- GA4-specific conventions reflected in the schema:
--   - "(not set)" is GA4's literal string for missing dimension values
--     (no campaign, no source, etc.). We use NOT NULL DEFAULT '(not set)' so
--     the UNIQUE constraint works without sentinel-string headaches.
--   - Bot exclusion is automatic in GA4 (IAB/ABC bot list, on by default).
--     The numbers we capture are already bot-filtered.
--   - GA4 uses "key_events" since mid-2024; "conversions" is the legacy alias.
--     Both work via the Data API. We use key_events as the column name.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. ga4_channels_daily — channel + UTM-detail rollup
-- =============================================================================

CREATE TABLE IF NOT EXISTS ga4_channels_daily (
    id                       BIGSERIAL       PRIMARY KEY,
    date                     DATE            NOT NULL,
    brand_id                 VARCHAR(64)     NOT NULL,

    -- Dimensions
    channel_group            VARCHAR(64)     NOT NULL DEFAULT '(not set)',
                              -- GA4 default channel grouping (Paid Social,
                              -- Organic Search, Direct, Email, Referral, etc.)
    source                   VARCHAR(255)    NOT NULL DEFAULT '(not set)',
    medium                   VARCHAR(128)    NOT NULL DEFAULT '(not set)',
    campaign                 VARCHAR(255)    NOT NULL DEFAULT '(not set)',
                              -- UTM campaign value; joinable to
                              -- ad_campaigns.campaign_name for Meta attribution

    -- Volume metrics
    sessions                 BIGINT          DEFAULT 0,
    engaged_sessions         BIGINT          DEFAULT 0,
    new_users                BIGINT          DEFAULT 0,
    active_users             BIGINT          DEFAULT 0,
    event_count              BIGINT          DEFAULT 0,

    -- Engagement
    engagement_rate          NUMERIC(8,4),
    average_session_duration NUMERIC(10,4),
    user_engagement_duration NUMERIC(12,4),
    bounce_rate              NUMERIC(8,4),

    -- Conversion + revenue
    key_events               BIGINT          DEFAULT 0,
                              -- GA4 "key events" (was "conversions" pre-2024)
    add_to_carts             BIGINT          DEFAULT 0,
    checkouts                BIGINT          DEFAULT 0,
                              -- begin_checkout event count
    ecommerce_purchases      BIGINT          DEFAULT 0,
    total_revenue            NUMERIC(12,4)   DEFAULT 0,
    purchase_revenue         NUMERIC(12,4)   DEFAULT 0,

    -- Derived
    conversion_rate          NUMERIC(8,4)
        GENERATED ALWAYS AS (
            CASE WHEN sessions > 0
                 THEN ecommerce_purchases::NUMERIC / sessions
                 ELSE NULL END
        ) STORED,
    aov_gbp                  NUMERIC(10,4)
        GENERATED ALWAYS AS (
            CASE WHEN ecommerce_purchases > 0
                 THEN total_revenue / ecommerce_purchases
                 ELSE NULL END
        ) STORED,

    synced_at                TIMESTAMPTZ,

    CONSTRAINT ga4_channels_daily_unique
        UNIQUE (date, brand_id, channel_group, source, medium, campaign)
);

CREATE INDEX IF NOT EXISTS idx_ga4_channels_date     ON ga4_channels_daily (date, brand_id);
CREATE INDEX IF NOT EXISTS idx_ga4_channels_channel  ON ga4_channels_daily (channel_group, brand_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_ga4_channels_campaign ON ga4_channels_daily (campaign, brand_id, date DESC)
    WHERE campaign <> '(not set)';

COMMENT ON TABLE ga4_channels_daily IS
    'Per-(date, channel, source, medium, campaign) GA4 metrics. Source of truth for top-level channel mix and UTM-based attribution. Bot-filtered by GA4. Joinable to ad_campaigns via campaign=campaign_name.';


-- =============================================================================
-- 2. ga4_products_daily — per-product channel attribution
-- =============================================================================
-- Grain: (date, item_id, channel_group). With Shopify-native GA4 integration,
-- item_id will be the Shopify product_id (joinable to product_catalogue and
-- ad_campaign_products.shopify_product_id). channel_group on the key lets
-- you slice "Bass Clef revenue: Paid Social vs Organic vs Email vs Direct".

CREATE TABLE IF NOT EXISTS ga4_products_daily (
    id                       BIGSERIAL       PRIMARY KEY,
    date                     DATE            NOT NULL,
    brand_id                 VARCHAR(64)     NOT NULL,

    -- Dimensions
    item_id                  VARCHAR(64)     NOT NULL DEFAULT '(not set)',
                              -- Shopify product_id when GA4 integration is
                              -- configured normally; sometimes variant_id
    item_name                VARCHAR(512),
    channel_group            VARCHAR(64)     NOT NULL DEFAULT '(not set)',

    -- Funnel counts
    item_views               BIGINT          DEFAULT 0,
                              -- view_item event count
    items_viewed             BIGINT          DEFAULT 0,
                              -- item view metric (different from event count)
    items_added_to_cart      BIGINT          DEFAULT 0,
    items_checked_out        BIGINT          DEFAULT 0,
    items_purchased          BIGINT          DEFAULT 0,
    item_purchase_quantity   BIGINT          DEFAULT 0,

    -- Revenue
    item_revenue             NUMERIC(12,4)   DEFAULT 0,

    -- Built-in GA4 rates (item-level)
    cart_to_view_rate        NUMERIC(8,4),
    purchase_to_view_rate    NUMERIC(8,4),

    synced_at                TIMESTAMPTZ,

    CONSTRAINT ga4_products_daily_unique
        UNIQUE (date, brand_id, item_id, channel_group)
);

CREATE INDEX IF NOT EXISTS idx_ga4_products_date    ON ga4_products_daily (date, brand_id);
CREATE INDEX IF NOT EXISTS idx_ga4_products_item    ON ga4_products_daily (item_id, brand_id);
CREATE INDEX IF NOT EXISTS idx_ga4_products_channel ON ga4_products_daily (channel_group, brand_id, date DESC);

COMMENT ON TABLE ga4_products_daily IS
    'Per-(date, item_id, channel) e-commerce metrics from GA4. Joinable to product_catalogue.product_id when GA4 integration uses product-level item_ids. Solves the cross-channel attribution problem.';


-- =============================================================================
-- 3. ga4_pages_daily — page-level engagement + landing diagnostics
-- =============================================================================

CREATE TABLE IF NOT EXISTS ga4_pages_daily (
    id                       BIGSERIAL       PRIMARY KEY,
    date                     DATE            NOT NULL,
    brand_id                 VARCHAR(64)     NOT NULL,

    page_path                VARCHAR(1024)   NOT NULL,
    page_title               VARCHAR(512),

    -- Volume
    screen_page_views        BIGINT          DEFAULT 0,
    sessions                 BIGINT          DEFAULT 0,
                              -- sessions that included this page
                              -- (overcounts vs total daily sessions across pages)
    entrances                BIGINT          DEFAULT 0,
                              -- sessions where this was the LANDING page
                              -- (no overcount across pages)
    active_users             BIGINT          DEFAULT 0,

    -- Engagement
    engaged_sessions         BIGINT          DEFAULT 0,
    engagement_rate          NUMERIC(8,4),
    bounce_rate              NUMERIC(8,4),
    average_session_duration NUMERIC(10,4),
    user_engagement_duration NUMERIC(12,4),

    synced_at                TIMESTAMPTZ,

    CONSTRAINT ga4_pages_daily_unique
        UNIQUE (date, brand_id, page_path)
);

CREATE INDEX IF NOT EXISTS idx_ga4_pages_date  ON ga4_pages_daily (date, brand_id);
CREATE INDEX IF NOT EXISTS idx_ga4_pages_path  ON ga4_pages_daily (page_path, brand_id);

COMMENT ON TABLE ga4_pages_daily IS
    'Per-(date, page_path) engagement metrics. entrances = landing page count. page_path of /products/<handle> is joinable to product_catalogue.product_handle for product-page diagnostics.';


-- =============================================================================
-- 4. v_ga4_funnel_daily — derived funnel view
-- =============================================================================
-- Aggregated from ga4_channels_daily. Per (date, channel_group), exposes
-- session-level funnel rates. No separate base table needed — the underlying
-- channels table has everything; this view just rolls up + computes ratios.

CREATE OR REPLACE VIEW v_ga4_funnel_daily AS
SELECT
    date,
    brand_id,
    channel_group,
    SUM(sessions)            AS sessions,
    SUM(engaged_sessions)    AS engaged_sessions,
    SUM(add_to_carts)        AS add_to_carts,
    SUM(checkouts)           AS checkouts,
    SUM(ecommerce_purchases) AS ecommerce_purchases,
    ROUND(SUM(total_revenue)::numeric, 4) AS total_revenue,

    -- Funnel rates
    ROUND((SUM(engaged_sessions)::numeric    / NULLIF(SUM(sessions), 0)) * 100, 2) AS engagement_rate_pct,
    ROUND((SUM(add_to_carts)::numeric        / NULLIF(SUM(sessions), 0)) * 100, 2) AS atc_rate_pct,
    ROUND((SUM(checkouts)::numeric           / NULLIF(SUM(add_to_carts), 0)) * 100, 2) AS atc_to_checkout_rate_pct,
    ROUND((SUM(ecommerce_purchases)::numeric / NULLIF(SUM(checkouts), 0)) * 100, 2) AS checkout_to_purchase_rate_pct,
    ROUND((SUM(ecommerce_purchases)::numeric / NULLIF(SUM(sessions), 0)) * 100, 2) AS overall_conv_rate_pct,
    ROUND((SUM(total_revenue) / NULLIF(SUM(ecommerce_purchases), 0))::numeric, 4)  AS aov_gbp
FROM ga4_channels_daily
GROUP BY date, brand_id, channel_group;

COMMENT ON VIEW v_ga4_funnel_daily IS
    'Per-(date, channel_group) funnel rollup with conversion-rate columns. Derived from ga4_channels_daily.';


COMMIT;

-- =============================================================================
-- Verify (run after applying)
-- =============================================================================
-- \d ga4_channels_daily
-- \d ga4_products_daily
-- \d ga4_pages_daily
-- \dv v_ga4_funnel_daily
