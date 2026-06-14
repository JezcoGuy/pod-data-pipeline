-- =============================================================================
-- Migration v8.1: Meta Marketing API support
-- =============================================================================
-- Three changes:
--   1. Extend ad_campaigns with full Meta payload coverage + raw_payload JSONB
--   2. Add product_handle to product_catalogue (for static-ad URL resolution)
--   3. Create ad_campaign_products (ad x product daily breakdown)
--
-- All changes are additive. Existing ad_campaigns rows are unaffected.
-- Wrapped in a transaction — if anything fails, nothing applies.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. EXTEND ad_campaigns
-- =============================================================================
-- Adds 30+ columns covering metadata, engagement, funnel actions, video
-- quartiles, quality rankings, attribution settings, destination URL, and a
-- raw_payload JSONB safety net for fields we haven't extracted yet.

ALTER TABLE ad_campaigns
    -- Hierarchy / metadata
    ADD COLUMN IF NOT EXISTS account_id              VARCHAR(64),
    ADD COLUMN IF NOT EXISTS account_currency        VARCHAR(8),
    ADD COLUMN IF NOT EXISTS account_name            VARCHAR(255),
    ADD COLUMN IF NOT EXISTS objective               VARCHAR(64),
    ADD COLUMN IF NOT EXISTS ad_status               VARCHAR(32),
    ADD COLUMN IF NOT EXISTS attribution_setting     VARCHAR(64),
    ADD COLUMN IF NOT EXISTS destination_url         TEXT,
    ADD COLUMN IF NOT EXISTS destination_type        VARCHAR(32)
        CHECK (destination_type IS NULL OR destination_type IN
            ('product','collection','homepage','other','dynamic')),

    -- Engagement extras
    ADD COLUMN IF NOT EXISTS unique_clicks           BIGINT          DEFAULT 0,

    -- Funnel actions (extracted from Meta's actions[] / action_values[] arrays;
    --   we use the omni_* variants where available — same denominator as
    --   purchase_roas, which is what Meta reports for ROAS)
    ADD COLUMN IF NOT EXISTS landing_page_views              BIGINT          DEFAULT 0,
    ADD COLUMN IF NOT EXISTS view_content_count              BIGINT          DEFAULT 0,
    ADD COLUMN IF NOT EXISTS view_content_value_gbp          NUMERIC(10,4)   DEFAULT 0,
    ADD COLUMN IF NOT EXISTS add_to_cart_count               BIGINT          DEFAULT 0,
    ADD COLUMN IF NOT EXISTS add_to_cart_value_gbp           NUMERIC(10,4)   DEFAULT 0,
    ADD COLUMN IF NOT EXISTS initiate_checkout_count         BIGINT          DEFAULT 0,
    ADD COLUMN IF NOT EXISTS initiate_checkout_value_gbp     NUMERIC(10,4)   DEFAULT 0,
    ADD COLUMN IF NOT EXISTS add_payment_info_count          BIGINT          DEFAULT 0,

    -- Video signal (Meta returns p25/p50/p75/p95/p100 — note 95 not 90)
    ADD COLUMN IF NOT EXISTS video_plays_25_pct      BIGINT          DEFAULT 0,
    ADD COLUMN IF NOT EXISTS video_plays_50_pct      BIGINT          DEFAULT 0,
    ADD COLUMN IF NOT EXISTS video_plays_75_pct      BIGINT          DEFAULT 0,
    ADD COLUMN IF NOT EXISTS video_plays_95_pct      BIGINT          DEFAULT 0,
    ADD COLUMN IF NOT EXISTS video_plays_100_pct     BIGINT          DEFAULT 0,
    ADD COLUMN IF NOT EXISTS video_avg_time_watched_sec NUMERIC(8,2),

    -- Quality rankings (Meta enum: UNKNOWN, BELOW_AVERAGE_*, AVERAGE, ABOVE_AVERAGE)
    ADD COLUMN IF NOT EXISTS quality_ranking         VARCHAR(32),
    ADD COLUMN IF NOT EXISTS engagement_rate_ranking VARCHAR(32),
    ADD COLUMN IF NOT EXISTS conversion_rate_ranking VARCHAR(32),

    -- Catch-all safety net: full /insights response for this row.
    -- Lets us add new typed columns later without re-syncing from Meta.
    ADD COLUMN IF NOT EXISTS raw_payload             JSONB;

-- Generated columns derived from the new fields.
-- (Existing generated columns cpc_raw, cpm, thumb_stop_ratio, meta_reported_roas
--  already live on the table — unchanged.)
ALTER TABLE ad_campaigns
    ADD COLUMN IF NOT EXISTS ctr NUMERIC(8,4)
        GENERATED ALWAYS AS (
            CASE WHEN impressions > 0 THEN clicks::NUMERIC / impressions ELSE NULL END
        ) STORED,
    ADD COLUMN IF NOT EXISTS unique_ctr NUMERIC(8,4)
        GENERATED ALWAYS AS (
            CASE WHEN impressions > 0 THEN unique_clicks::NUMERIC / impressions ELSE NULL END
        ) STORED,
    ADD COLUMN IF NOT EXISTS hook_rate NUMERIC(8,4)
        GENERATED ALWAYS AS (
            CASE WHEN impressions > 0 THEN video_plays_25_pct::NUMERIC / impressions ELSE NULL END
        ) STORED;

-- Helpful indexes for the new shape.
CREATE INDEX IF NOT EXISTS idx_ad_campaigns_date_brand   ON ad_campaigns (date, brand_id);
CREATE INDEX IF NOT EXISTS idx_ad_campaigns_ad_brand     ON ad_campaigns (ad_id, brand_id);
CREATE INDEX IF NOT EXISTS idx_ad_campaigns_campaign     ON ad_campaigns (campaign_id, brand_id);
CREATE INDEX IF NOT EXISTS idx_ad_campaigns_destination  ON ad_campaigns (destination_type)
    WHERE destination_type IS NOT NULL;

-- GIN index on raw_payload — only worth creating if we expect to query
-- arbitrary JSON keys ad-hoc. Skipping for now; can add later.

COMMENT ON COLUMN ad_campaigns.destination_url IS
    'Static-ad destination URL parsed from creative.object_story_spec.link_data.link. NULL for catalogue/dynamic ads.';
COMMENT ON COLUMN ad_campaigns.destination_type IS
    'Derived from destination_url: product/collection/homepage/other/dynamic.';
COMMENT ON COLUMN ad_campaigns.raw_payload IS
    'Full /insights response for this ad-day. Enables retroactive schema extension via ALTER TABLE + UPDATE without re-syncing from Meta.';


-- =============================================================================
-- 2. EXTEND product_catalogue
-- =============================================================================
-- Adds product_handle so static-ad destination URLs can resolve to product_id
-- (URL .../products/<handle> -> handle -> product_id). Populated by the
-- forthcoming shopify_products_sync.py from each product's `handle` field.

ALTER TABLE product_catalogue
    ADD COLUMN IF NOT EXISTS product_handle VARCHAR(255);

CREATE INDEX IF NOT EXISTS idx_product_catalogue_handle
    ON product_catalogue (product_handle, brand_id)
    WHERE product_handle IS NOT NULL;

COMMENT ON COLUMN product_catalogue.product_handle IS
    'Shopify product handle (slug). Used to resolve static-ad destination URLs to product_id.';


-- =============================================================================
-- 3. CREATE ad_campaign_products
-- =============================================================================
-- Grain: one row per (date, ad_id, product) tuple.
-- Scope: CATALOGUE ADS ONLY. Static-ad spend stays in ad_campaigns; static
-- attribution is done downstream by joining orders.utm_* against ad_campaigns
-- in a Metabase view (UTM-based attribution, not product-level).
--
-- Each row represents a single Shopify variant that a Meta catalogue ad served
-- on a given day, with spend/impressions/conversions allocated per variant by
-- Meta's product_id breakdown.
--
-- Reconciliation: SUM(ad_campaign_products.spend WHERE ad_id=X) per ad-day
-- equals ad_campaigns.spend for that ad-day, for catalogue ads (modulo Meta
-- rounding). Static ads have no rows here — their spend lives only in
-- ad_campaigns.

CREATE TABLE IF NOT EXISTS ad_campaign_products (
    id                          BIGSERIAL       PRIMARY KEY,

    date                        DATE            NOT NULL,
    brand_id                    VARCHAR(64)     NOT NULL,
    platform                    VARCHAR(32)     NOT NULL DEFAULT 'meta'
        CHECK (platform IN ('meta','google','tiktok','pinterest','other')),

    -- Hierarchy (denormalised from ad_campaigns for query speed; no FK enforced)
    ad_id                       VARCHAR(128)    NOT NULL,
    campaign_id                 VARCHAR(128),
    adset_id                    VARCHAR(128),

    -- Product attribution
    meta_product_id             VARCHAR(255)    NOT NULL,
                                -- raw compound from Meta: "<variant_id>, <product_title>"
    meta_variant_id             VARCHAR(64),
                                -- parsed numeric portion (= Shopify variant_id for Shopify-native feeds)
    shopify_product_id          VARCHAR(64),
                                -- resolved via product_catalogue.variant_id; NULL until products_sync runs
    shopify_variant_id          VARCHAR(64),
                                -- = meta_variant_id when format matches
    product_title               VARCHAR(512),
                                -- from Meta's compound payload (always available), overwritten by product_catalogue when resolved
    variant_title               VARCHAR(256),
                                -- from product_catalogue when resolved
    product_handle              VARCHAR(255),
                                -- from product_catalogue when resolved; useful for joining to static-ad URL queries

    attribution_source          VARCHAR(32)     NOT NULL
        CHECK (attribution_source IN ('meta_catalog','unresolved')),
                                -- meta_catalog: row came from Meta's product_id breakdown AND product_catalogue lookup succeeded
                                -- unresolved: row came from Meta's breakdown BUT product_catalogue lookup failed (new variant, not yet synced)
    match_method                VARCHAR(32),
                                -- variant_id_exact (default), sku, name_fuzzy, or NULL if unresolved

    -- Metrics: per-variant allocation from Meta's product_id breakdown call
    spend_gbp                   NUMERIC(10,4)   DEFAULT 0,
    impressions                 BIGINT          DEFAULT 0,
    clicks                      BIGINT          DEFAULT 0,
    link_clicks                 BIGINT          DEFAULT 0,
    meta_reported_purchases     BIGINT          DEFAULT 0,
    meta_reported_revenue       NUMERIC(10,4)   DEFAULT 0,
    meta_reported_roas          NUMERIC(10,4)
        GENERATED ALWAYS AS (
            CASE WHEN spend_gbp > 0
                 THEN meta_reported_revenue / spend_gbp
                 ELSE NULL END
        ) STORED,

    synced_at                   TIMESTAMPTZ,

    CONSTRAINT ad_campaign_products_unique
        UNIQUE (date, brand_id, platform, ad_id, meta_product_id)
);

CREATE INDEX IF NOT EXISTS idx_acp_date_brand
    ON ad_campaign_products (date, brand_id);
CREATE INDEX IF NOT EXISTS idx_acp_shopify_product
    ON ad_campaign_products (shopify_product_id, brand_id)
    WHERE shopify_product_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_acp_shopify_variant
    ON ad_campaign_products (shopify_variant_id, brand_id)
    WHERE shopify_variant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_acp_ad
    ON ad_campaign_products (ad_id, date);
CREATE INDEX IF NOT EXISTS idx_acp_campaign
    ON ad_campaign_products (campaign_id, brand_id)
    WHERE campaign_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_acp_source
    ON ad_campaign_products (attribution_source);

COMMENT ON TABLE ad_campaign_products IS
    'CATALOGUE ADS ONLY. One row per (date, ad_id, variant) tuple from Meta product_id breakdown. Static ads do not write here — their attribution is via orders.utm_* joined to ad_campaigns in a Metabase view.';
COMMENT ON COLUMN ad_campaign_products.meta_product_id IS
    'Raw compound string from Meta breakdown, e.g. "56601510314308, Retro Tube T-Shirt".';
COMMENT ON COLUMN ad_campaign_products.meta_variant_id IS
    'Parsed numeric portion of meta_product_id. For Shopify-native Meta catalogue feed = Shopify variant_id.';
COMMENT ON COLUMN ad_campaign_products.attribution_source IS
    'meta_catalog: variant resolved against product_catalogue. unresolved: variant not yet in product_catalogue (new design, products_sync hasn''t caught up).';


COMMIT;

-- =============================================================================
-- Verify (run after applying)
-- =============================================================================
-- \d ad_campaigns
-- \d ad_campaign_products
-- \d product_catalogue
-- SELECT COUNT(*) FROM ad_campaigns;        -- existing rows preserved
-- SELECT COUNT(*) FROM ad_campaign_products; -- 0 (new table)
