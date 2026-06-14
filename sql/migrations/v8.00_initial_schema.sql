-- =============================================================================
-- Your Brand Data Pipeline — Complete PostgreSQL Schema
-- Version: 7.0
-- Last Updated: May 2026
-- =============================================================================
-- v7.0 changes from v6.0:
-- - orders: financial_status, refund_amount_gbp, refund_by_gateway_json, refunded_at added
-- - returns: redesigned — shopify/paypal/klarna refund fields removed
--            shopify_refund_amount, gelato_wallet_credit, is_exchange added
--            net_cost recalculated: refund - gelato_credit + reorder + handling
-- - fulfilments: status constraint expanded for all known Gelato + Printify statuses
-- - Shopify backfill complete: financial_status populated for all 13,934 orders
--   paid: 13,727 | partially_refunded: 117 | refunded: 84 | partially_paid: 6
--   Total refunds to customers: £6,561 across 201 orders
-- =============================================================================

SET search_path TO public;

-- =============================================================================
-- 1. ORDERS
-- =============================================================================

CREATE TABLE IF NOT EXISTS orders (
    order_id                VARCHAR(64)     PRIMARY KEY,
    brand_id                VARCHAR(64)     NOT NULL,
    created_at              TIMESTAMPTZ     NOT NULL,
    order_name              VARCHAR(64),
    synced_at               TIMESTAMPTZ,

    -- Revenue
    revenue_gbp             NUMERIC(10,4)   NOT NULL DEFAULT 0,
    subtotal_gbp            NUMERIC(10,4),
    shipping_charged_gbp    NUMERIC(10,4),
    total_tax_gbp           NUMERIC(10,4),
    tax_lines_json          JSONB,
    revenue_presentment     NUMERIC(10,4),
    presentment_currency    VARCHAR(8),

    -- Discounts
    discount_amount_gbp     NUMERIC(10,4)   DEFAULT 0,
    discount_code           VARCHAR(128),
    discount_type           VARCHAR(64),
    discount_value          NUMERIC(10,4),

    -- Payment & fees
    payment_gateway         VARCHAR(64),
    shopify_fee_gbp         NUMERIC(10,4),
    shopify_fee_pct         NUMERIC(6,4),
    paypal_settle_amount    NUMERIC(10,4),
    paypal_fee_amount       NUMERIC(10,4),
    klarna_fee_gbp          NUMERIC(10,4),
    total_payment_fees      NUMERIC(10,4)
        GENERATED ALWAYS AS (
            COALESCE(shopify_fee_gbp, 0) +
            COALESCE(paypal_fee_amount, 0) +
            COALESCE(klarna_fee_gbp, 0)
        ) STORED,

    -- Financial status & refunds (from Shopify API — source of truth)
    financial_status        VARCHAR(32),
    refund_amount_gbp       NUMERIC(10,4)   DEFAULT 0,
    refund_by_gateway_json  JSONB,
    refunded_at             TIMESTAMPTZ,

    -- Shipping / Location
    shipping_country_code   VARCHAR(8),
    shipping_country_name   VARCHAR(128),
    shipping_province       VARCHAR(128),
    shipping_zip            VARCHAR(32),

    -- Customer
    customer_id             VARCHAR(64),
    customer_email          VARCHAR(256),
    customer_name           VARCHAR(256),
    customer_orders_count   INTEGER         DEFAULT 1,
    is_new_customer         BOOLEAN         DEFAULT TRUE,

    -- UTM / Attribution
    landing_site            TEXT,
    referring_site          TEXT,
    utm_source              VARCHAR(256),
    utm_medium              VARCHAR(256),
    utm_campaign            VARCHAR(256),
    utm_content             VARCHAR(256),
    utm_term                VARCHAR(256),

    -- COGS
    cogs_gbp                NUMERIC(10,4),
    cogs_gbp_incl_vat       NUMERIC(10,4),
    cogs_gbp_excl_vat       NUMERIC(10,4),
    cogs_status             VARCHAR(32)     DEFAULT 'pending'
                                CHECK (cogs_status IN (
                                    'pending','estimated','final',
                                    'manual_override','cancelled'
                                )),
    cogs_updated_at         TIMESTAMPTZ,

    -- Fulfilment (provider agnostic)
    fulfillment_match_status VARCHAR(32)    DEFAULT 'unmatched'
                                CHECK (fulfillment_match_status IN ('matched','unmatched','manual')),
    fulfillment_order_id    VARCHAR(128),
    fulfillment_provider    VARCHAR(32)
                                CHECK (fulfillment_provider IN ('gelato','printify','manual','other')),

    line_items_count        INTEGER         DEFAULT 0,
    override_flag           BOOLEAN         DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_orders_brand_date           ON orders (brand_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_customer_email       ON orders (customer_email, brand_id);
CREATE INDEX IF NOT EXISTS idx_orders_gateway              ON orders (payment_gateway, brand_id);
CREATE INDEX IF NOT EXISTS idx_orders_fulfillment_status   ON orders (fulfillment_match_status, brand_id) WHERE fulfillment_match_status = 'unmatched';
CREATE INDEX IF NOT EXISTS idx_orders_cogs_status          ON orders (cogs_status, brand_id) WHERE cogs_status IN ('pending','estimated');
CREATE INDEX IF NOT EXISTS idx_orders_country              ON orders (shipping_country_code, brand_id);
CREATE INDEX IF NOT EXISTS idx_orders_financial_status     ON orders (financial_status, brand_id);
CREATE INDEX IF NOT EXISTS idx_orders_provider             ON orders (fulfillment_provider, brand_id);

COMMENT ON COLUMN orders.financial_status IS 'Shopify financial status: paid/refunded/partially_refunded/partially_paid/voided';
COMMENT ON COLUMN orders.refund_amount_gbp IS 'Total refunded to customer in GBP. Source: Shopify refund transactions.';
COMMENT ON COLUMN orders.refund_by_gateway_json IS 'Refund breakdown by gateway e.g. {"shopify_payments": 25.11}';
COMMENT ON COLUMN orders.refunded_at IS 'Timestamp of first refund processed';
COMMENT ON COLUMN orders.cogs_gbp IS 'Active COGS — mirrors cogs_gbp_incl_vat. Switch to excl_vat when reclaiming VAT.';
COMMENT ON COLUMN orders.fulfillment_match_status IS 'Provider agnostic (was gelato_match_status)';
COMMENT ON COLUMN orders.fulfillment_provider IS 'gelato / printify / manual / other';

-- =============================================================================
-- 2. LINE ITEMS
-- =============================================================================

CREATE TABLE IF NOT EXISTS line_items (
    line_item_id        VARCHAR(64)     PRIMARY KEY,
    order_id            VARCHAR(64)     NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    brand_id            VARCHAR(64)     NOT NULL,
    product_id          VARCHAR(64),
    variant_id          VARCHAR(64),
    product_title       VARCHAR(512),
    variant_title       VARCHAR(256),
    quantity            INTEGER         NOT NULL DEFAULT 1,
    unit_price_gbp      NUMERIC(10,4)   NOT NULL DEFAULT 0,
    line_total_gbp      NUMERIC(10,4)   NOT NULL DEFAULT 0,
    line_cogs_gbp       NUMERIC(10,4)
);

CREATE INDEX IF NOT EXISTS idx_line_items_order    ON line_items (order_id);
CREATE INDEX IF NOT EXISTS idx_line_items_product  ON line_items (product_id, brand_id);

-- =============================================================================
-- 3. GELATO ORDERS (audit trail)
-- =============================================================================

CREATE TABLE IF NOT EXISTS gelato_orders (
    gelato_order_id         VARCHAR(128)    PRIMARY KEY,
    shopify_order_id        VARCHAR(64)     REFERENCES orders(order_id) ON DELETE SET NULL,
    brand_id                VARCHAR(64)     NOT NULL,
    cogs_gbp                NUMERIC(10,4),
    cogs_gbp_incl_vat       NUMERIC(10,4),
    cogs_gbp_excl_vat       NUMERIC(10,4),
    products_price          NUMERIC(10,4),
    shipping_price          NUMERIC(10,4),
    discount_amount         NUMERIC(10,4),
    vat_amount              NUMERIC(10,4),
    receipt_number          VARCHAR(128),
    status                  VARCHAR(64),
    created_at              TIMESTAMPTZ,
    synced_at               TIMESTAMPTZ,
    tracking_code           VARCHAR(256),
    tracking_url            TEXT,
    carrier                 VARCHAR(128),
    shipped_at              TIMESTAMPTZ,
    min_delivery_date       DATE,
    max_delivery_date       DATE,
    fulfillment_country     VARCHAR(8)
);

CREATE INDEX IF NOT EXISTS idx_gelato_shopify_order ON gelato_orders (shopify_order_id);

-- =============================================================================
-- 4. FULFILMENTS — UNIFIED PROVIDER AGNOSTIC
-- =============================================================================

CREATE TABLE IF NOT EXISTS fulfilments (
    fulfilment_id           BIGSERIAL       PRIMARY KEY,
    brand_id                VARCHAR(64)     NOT NULL,
    shopify_order_id        VARCHAR(64)     REFERENCES orders(order_id) ON DELETE SET NULL,
    order_name              VARCHAR(64),

    provider                VARCHAR(32)     NOT NULL
                                CHECK (provider IN ('gelato','printify','manual','other')),
    provider_order_id       VARCHAR(128),

    -- Status (covers all known Gelato + Printify statuses)
    fulfilment_status       VARCHAR(64),
    is_cancelled            BOOLEAN         DEFAULT FALSE,

    -- COGS
    cogs_gbp_incl_vat       NUMERIC(10,4),
    cogs_gbp_excl_vat       NUMERIC(10,4),
    products_price          NUMERIC(10,4),
    shipping_price          NUMERIC(10,4),
    discount_amount         NUMERIC(10,4),
    vat_amount              NUMERIC(10,4),
    receipt_number          VARCHAR(128),

    -- Tracking
    tracking_number         VARCHAR(256),
    tracking_url            TEXT,
    carrier                 VARCHAR(128),
    shipping_method         VARCHAR(256),

    -- Dates
    order_placed_at         TIMESTAMPTZ,
    dispatched_at           TIMESTAMPTZ,
    estimated_delivery_at   TIMESTAMPTZ,
    delivered_at            TIMESTAMPTZ,

    -- Auto-calculated
    hours_to_dispatch       NUMERIC(8,2)
        GENERATED ALWAYS AS (
            CASE WHEN dispatched_at IS NOT NULL AND order_placed_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM (dispatched_at - order_placed_at)) / 3600
            ELSE NULL END
        ) STORED,
    hours_to_delivery       NUMERIC(8,2)
        GENERATED ALWAYS AS (
            CASE WHEN delivered_at IS NOT NULL AND order_placed_at IS NOT NULL
            THEN EXTRACT(EPOCH FROM (delivered_at - order_placed_at)) / 3600
            ELSE NULL END
        ) STORED,

    -- Location
    destination_country     VARCHAR(64),
    destination_region      VARCHAR(64),
    fulfillment_country     VARCHAR(8),

    -- Alert flags
    dispatch_alert_sent     BOOLEAN         DEFAULT FALSE,
    delivery_alert_sent     BOOLEAN         DEFAULT FALSE,
    is_late_dispatch        BOOLEAN         DEFAULT FALSE,
    is_late_delivery        BOOLEAN         DEFAULT FALSE,
    override_flag           BOOLEAN         DEFAULT FALSE,

    provider_data_json      JSONB,
    synced_at               TIMESTAMPTZ     DEFAULT NOW(),

    CONSTRAINT fulfilments_unique UNIQUE (provider, provider_order_id)
);

CREATE INDEX IF NOT EXISTS idx_fulfilments_shopify_order ON fulfilments (shopify_order_id);
CREATE INDEX IF NOT EXISTS idx_fulfilments_provider      ON fulfilments (provider, brand_id);
CREATE INDEX IF NOT EXISTS idx_fulfilments_status        ON fulfilments (fulfilment_status, brand_id);
CREATE INDEX IF NOT EXISTS idx_fulfilments_country       ON fulfilments (destination_country, brand_id);

COMMENT ON TABLE fulfilments IS 'Unified fulfilment table. Provider agnostic. Gelato + Printify live.';
COMMENT ON COLUMN fulfilments.vat_amount IS 'Tax charged. UK=reclaimable VAT. US=sales tax (not reclaimable). Use destination_country to distinguish.';
COMMENT ON COLUMN fulfilments.override_flag IS 'TRUE = script will not overwrite. Set in NocoDB for manual corrections.';

-- =============================================================================
-- 5. ABANDONED CARTS
-- =============================================================================

CREATE TABLE IF NOT EXISTS abandoned_carts (
    checkout_id         VARCHAR(64)     PRIMARY KEY,
    brand_id            VARCHAR(64)     NOT NULL,
    created_at          TIMESTAMPTZ     NOT NULL,
    recovered_at        TIMESTAMPTZ,
    status              VARCHAR(32)     NOT NULL DEFAULT 'abandoned'
                            CHECK (status IN ('abandoned','recovered')),
    cart_value          NUMERIC(10,4),
    recovered_value     NUMERIC(10,4),
    customer_email      VARCHAR(256),
    country_code        VARCHAR(8),
    line_items_json     JSONB,
    klaviyo_attributed  BOOLEAN         DEFAULT FALSE,
    synced_at           TIMESTAMPTZ
);

-- =============================================================================
-- 6. CUSTOMERS
-- =============================================================================

CREATE TABLE IF NOT EXISTS customers (
    customer_email      VARCHAR(256)    NOT NULL,
    brand_id            VARCHAR(64)     NOT NULL,
    customer_id         VARCHAR(64),
    first_seen_at       TIMESTAMPTZ,
    last_seen_at        TIMESTAMPTZ,
    total_orders        INTEGER         DEFAULT 0,
    total_revenue_gbp   NUMERIC(10,4)   DEFAULT 0,
    total_cogs_gbp      NUMERIC(10,4)   DEFAULT 0,
    revenue_ltv         NUMERIC(10,4)   DEFAULT 0,
    profit_ltv          NUMERIC(10,4)   DEFAULT 0,
    ltv_updated_at      TIMESTAMPTZ,
    PRIMARY KEY (customer_email, brand_id)
);

-- =============================================================================
-- 7. AD CAMPAIGNS
-- =============================================================================

CREATE TABLE IF NOT EXISTS ad_campaigns (
    id                      BIGSERIAL       PRIMARY KEY,
    date                    DATE            NOT NULL,
    brand_id                VARCHAR(64)     NOT NULL,
    platform                VARCHAR(32)     NOT NULL
                                CHECK (platform IN ('meta','google','tiktok','pinterest','other')),
    campaign_id             VARCHAR(128),
    campaign_name           VARCHAR(512),
    adset_id                VARCHAR(128),
    adset_name              VARCHAR(512),
    ad_id                   VARCHAR(128),
    ad_name                 VARCHAR(512),
    spend_gbp               NUMERIC(10,4)   DEFAULT 0,
    impressions             BIGINT          DEFAULT 0,
    reach                   BIGINT          DEFAULT 0,
    frequency               NUMERIC(8,4),
    clicks                  BIGINT          DEFAULT 0,
    link_clicks             BIGINT          DEFAULT 0,
    outbound_clicks         BIGINT          DEFAULT 0,
    cpc_raw                 NUMERIC(10,4)
        GENERATED ALWAYS AS (
            CASE WHEN clicks > 0 THEN spend_gbp / clicks ELSE NULL END
        ) STORED,
    cpm                     NUMERIC(10,4)
        GENERATED ALWAYS AS (
            CASE WHEN impressions > 0 THEN (spend_gbp / impressions) * 1000 ELSE NULL END
        ) STORED,
    video_plays             BIGINT          DEFAULT 0,
    thumb_stop_ratio        NUMERIC(8,4)
        GENERATED ALWAYS AS (
            CASE WHEN impressions > 0 THEN video_plays::NUMERIC / impressions ELSE NULL END
        ) STORED,
    meta_reported_purchases BIGINT          DEFAULT 0,
    meta_reported_revenue   NUMERIC(10,4)   DEFAULT 0,
    meta_reported_roas      NUMERIC(10,4)
        GENERATED ALWAYS AS (
            CASE WHEN spend_gbp > 0 THEN meta_reported_revenue / spend_gbp ELSE NULL END
        ) STORED,
    synced_at               TIMESTAMPTZ,
    CONSTRAINT ad_campaigns_unique UNIQUE (date, brand_id, platform, ad_id)
);

-- =============================================================================
-- 8. EMAIL CAMPAIGNS
-- =============================================================================

CREATE TABLE IF NOT EXISTS email_campaigns (
    id                  BIGSERIAL       PRIMARY KEY,
    date                DATE            NOT NULL,
    brand_id            VARCHAR(64)     NOT NULL,
    campaign_id         VARCHAR(128)    NOT NULL,
    campaign_name       VARCHAR(512),
    campaign_type       VARCHAR(32)     NOT NULL CHECK (campaign_type IN ('flow','campaign')),
    flow_id             VARCHAR(128),
    flow_name           VARCHAR(512),
    emails_sent         BIGINT          DEFAULT 0,
    emails_delivered    BIGINT          DEFAULT 0,
    unique_opens        BIGINT          DEFAULT 0,
    open_rate           NUMERIC(8,4)
        GENERATED ALWAYS AS (
            CASE WHEN emails_delivered > 0
            THEN unique_opens::NUMERIC / emails_delivered ELSE NULL END
        ) STORED,
    unique_clicks       BIGINT          DEFAULT 0,
    click_rate          NUMERIC(8,4)
        GENERATED ALWAYS AS (
            CASE WHEN emails_delivered > 0
            THEN unique_clicks::NUMERIC / emails_delivered ELSE NULL END
        ) STORED,
    revenue_attributed  NUMERIC(10,4)   DEFAULT 0,
    unsubscribes        BIGINT          DEFAULT 0,
    bounce_rate         NUMERIC(8,4),
    metric_status       VARCHAR(32)     DEFAULT 'updating'
                            CHECK (metric_status IN ('updating','final')),
    synced_at           TIMESTAMPTZ,
    CONSTRAINT email_campaigns_unique UNIQUE (date, brand_id, campaign_id)
);

-- =============================================================================
-- 9. RETURNS (v7.0 redesign)
-- Financial data automated from Shopify. Operational data manual via form.
-- =============================================================================

CREATE TABLE IF NOT EXISTS returns (
    return_id               BIGSERIAL       PRIMARY KEY,
    brand_id                VARCHAR(64)     NOT NULL,
    order_id                VARCHAR(64)     REFERENCES orders(order_id) ON DELETE SET NULL,
    customer_name           VARCHAR(256),
    customer_email          VARCHAR(256),
    date_reported           DATE            NOT NULL DEFAULT CURRENT_DATE,
    date_resolved           DATE,
    reason                  VARCHAR(64)
                                CHECK (reason IN (
                                    'changed_mind','shipping_issue','wrong_item',
                                    'wrong_size','print_quality','goodwill','discount'
                                )),
    status                  VARCHAR(32)     NOT NULL DEFAULT 'open'
                                CHECK (status IN ('open','in_progress','done')),

    -- Financial (Shopify API is source of truth — denormalised here for net_cost calc)
    shopify_refund_amount   NUMERIC(10,4)   DEFAULT 0,

    -- Manual entry by wife/VA
    gelato_wallet_credit    NUMERIC(10,4)   DEFAULT 0,
    gelato_reorder_cost     NUMERIC(10,4)   DEFAULT 0,
    handling_shipping_absorbed NUMERIC(10,4) DEFAULT 0,
    is_exchange             BOOLEAN         DEFAULT FALSE,

    -- Auto-calculated
    net_cost                NUMERIC(10,4)
        GENERATED ALWAYS AS (
            COALESCE(shopify_refund_amount, 0) -
            COALESCE(gelato_wallet_credit, 0) +
            COALESCE(gelato_reorder_cost, 0) +
            COALESCE(handling_shipping_absorbed, 0)
        ) STORED,

    item_description        TEXT,
    notes                   TEXT,
    action_items            TEXT,
    override_flag           BOOLEAN         DEFAULT FALSE,
    created_at              TIMESTAMPTZ     DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_returns_brand_date ON returns (brand_id, date_reported DESC);
CREATE INDEX IF NOT EXISTS idx_returns_status     ON returns (status, brand_id) WHERE status IN ('open','in_progress');

COMMENT ON TABLE returns IS 'v7.0: Financial data from Shopify API (automated). Operational data manual via NocoDB form. net_cost = refund - gelato_credit + reorder + handling.';
COMMENT ON COLUMN returns.shopify_refund_amount IS 'Denormalised from orders.refund_amount_gbp for net_cost calc. Populated when wife enters order number in form.';
COMMENT ON COLUMN returns.gelato_wallet_credit IS 'Amount Gelato credited to wallet (manual entry — no API available)';
COMMENT ON COLUMN returns.is_exchange IS 'TRUE if exchange order was created for this return';

-- =============================================================================
-- 10. EXPENSES
-- =============================================================================

CREATE TABLE IF NOT EXISTS expenses (
    expense_id      BIGSERIAL       PRIMARY KEY,
    brand_id        VARCHAR(64)     NOT NULL,
    date            DATE            NOT NULL,
    name            VARCHAR(256)    NOT NULL,
    category        VARCHAR(64)
                        CHECK (category IN (
                            'subscription','fulfilment','wages',
                            'services','office','meals','other'
                        )),
    amount          NUMERIC(10,4)   NOT NULL,
    payment_method  VARCHAR(64),
    vendor          VARCHAR(256),
    notes           TEXT,
    source          VARCHAR(32)     DEFAULT 'manual'
                        CHECK (source IN ('manual','xero')),
    override_flag   BOOLEAN         DEFAULT FALSE,
    created_at      TIMESTAMPTZ     DEFAULT NOW()
);

-- =============================================================================
-- 11. DAILY CONTENT
-- =============================================================================

CREATE TABLE IF NOT EXISTS daily_content (
    id              BIGSERIAL       PRIMARY KEY,
    date            DATE            NOT NULL,
    brand_id        VARCHAR(64)     NOT NULL,
    designs_uploaded    INTEGER     DEFAULT 0,
    ads_launched        INTEGER     DEFAULT 0,
    campaigns_sent      INTEGER     DEFAULT 0,
    fb_posts            INTEGER     DEFAULT 0,
    ig_posts            INTEGER     DEFAULT 0,
    notes               TEXT,
    submitted_by        VARCHAR(128),
    submitted_at        TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT daily_content_unique UNIQUE (date, brand_id)
);

-- =============================================================================
-- 12. PRODUCT CATALOGUE
-- =============================================================================

CREATE TABLE IF NOT EXISTS product_catalogue (
    product_id          VARCHAR(64)     NOT NULL,
    variant_id          VARCHAR(64)     NOT NULL,
    brand_id            VARCHAR(64)     NOT NULL,
    product_title       VARCHAR(512),
    product_type        VARCHAR(128),
    variant_title       VARCHAR(256),
    default_cogs_gbp    NUMERIC(10,4)   DEFAULT 10.50,
    gelato_product_id   VARCHAR(128),
    active              BOOLEAN         DEFAULT TRUE,
    synced_at           TIMESTAMPTZ,
    PRIMARY KEY (product_id, variant_id, brand_id)
);

-- =============================================================================
-- 13. COUNTRY REGIONS
-- =============================================================================

CREATE TABLE IF NOT EXISTS country_regions (
    country_code    VARCHAR(8)      PRIMARY KEY,
    country_name    VARCHAR(128)    NOT NULL,
    region          VARCHAR(64)     NOT NULL
                        CHECK (region IN ('Europe','North America','Oceania','Asia','Other')),
    currency_code   VARCHAR(8)
);

INSERT INTO country_regions (country_code, country_name, region, currency_code) VALUES
    ('GB','United Kingdom','Europe','GBP'),('DE','Germany','Europe','EUR'),
    ('FR','France','Europe','EUR'),('NL','Netherlands','Europe','EUR'),
    ('SE','Sweden','Europe','SEK'),('NO','Norway','Europe','NOK'),
    ('DK','Denmark','Europe','DKK'),('FI','Finland','Europe','EUR'),
    ('IE','Ireland','Europe','EUR'),('ES','Spain','Europe','EUR'),
    ('IT','Italy','Europe','EUR'),('PT','Portugal','Europe','EUR'),
    ('BE','Belgium','Europe','EUR'),('AT','Austria','Europe','EUR'),
    ('CH','Switzerland','Europe','CHF'),('PL','Poland','Europe','PLN'),
    ('US','United States','North America','USD'),('CA','Canada','North America','CAD'),
    ('MX','Mexico','North America','MXN'),('AU','Australia','Oceania','AUD'),
    ('NZ','New Zealand','Oceania','NZD'),('JP','Japan','Asia','JPY'),
    ('SG','Singapore','Asia','SGD'),('HK','Hong Kong','Asia','HKD')
ON CONFLICT (country_code) DO NOTHING;

-- =============================================================================
-- 14. DAILY SUMMARY
-- =============================================================================

CREATE TABLE IF NOT EXISTS daily_summary (
    date        DATE            NOT NULL,
    brand_id    VARCHAR(64)     NOT NULL,
    revenue_gross               NUMERIC(10,4)   DEFAULT 0,
    revenue_shopify_payments    NUMERIC(10,4)   DEFAULT 0,
    revenue_paypal              NUMERIC(10,4)   DEFAULT 0,
    revenue_klarna              NUMERIC(10,4)   DEFAULT 0,
    revenue_other               NUMERIC(10,4)   DEFAULT 0,
    cogs_total                  NUMERIC(10,4)   DEFAULT 0,
    cogs_total_excl_vat         NUMERIC(10,4)   DEFAULT 0,
    gelato_discount_total       NUMERIC(10,4)   DEFAULT 0,
    returns_total_cost          NUMERIC(10,4)   DEFAULT 0,
    gross_profit                NUMERIC(10,4)   DEFAULT 0,
    gross_margin_pct            NUMERIC(8,4),
    overheads_actual            NUMERIC(10,4)   DEFAULT 0,
    total_ad_spend              NUMERIC(10,4)   DEFAULT 0,
    mer                         NUMERIC(10,4),
    sessions                    BIGINT          DEFAULT 0,
    orders_count                INTEGER         DEFAULT 0,
    conversion_rate             NUMERIC(8,4),
    aov                         NUMERIC(10,4),
    new_customers_count         INTEGER         DEFAULT 0,
    returning_customers_count   INTEGER         DEFAULT 0,
    returns_count               INTEGER         DEFAULT 0,
    returns_net_cost            NUMERIC(10,4)   DEFAULT 0,
    summary_version             INTEGER         DEFAULT 1,
    calculated_at               TIMESTAMPTZ     DEFAULT NOW(),
    PRIMARY KEY (date, brand_id)
);

-- =============================================================================
-- 15. MONTHLY SUMMARY
-- =============================================================================

CREATE TABLE IF NOT EXISTS monthly_summary (
    month           VARCHAR(7)      NOT NULL,
    brand_id        VARCHAR(64)     NOT NULL,
    revenue_total               NUMERIC(12,4)   DEFAULT 0,
    cogs_total                  NUMERIC(12,4)   DEFAULT 0,
    cogs_total_excl_vat         NUMERIC(12,4)   DEFAULT 0,
    gross_profit                NUMERIC(12,4)   DEFAULT 0,
    gross_margin_pct            NUMERIC(8,4),
    total_expenses              NUMERIC(12,4)   DEFAULT 0,
    net_profit                  NUMERIC(12,4)   DEFAULT 0,
    total_ad_spend              NUMERIC(12,4)   DEFAULT 0,
    mer_month                   NUMERIC(8,4),
    total_returns               INTEGER         DEFAULT 0,
    return_rate_month           NUMERIC(8,4),
    calculated_at               TIMESTAMPTZ     DEFAULT NOW(),
    PRIMARY KEY (month, brand_id)
);

-- =============================================================================
-- 16. SYNC LOGS
-- =============================================================================

CREATE TABLE IF NOT EXISTS sync_logs (
    id              BIGSERIAL       PRIMARY KEY,
    brand_id        VARCHAR(64)     NOT NULL,
    script          VARCHAR(128)    NOT NULL,
    status          VARCHAR(32)     NOT NULL CHECK (status IN ('started','completed','failed')),
    records_processed INTEGER       DEFAULT 0,
    errors          INTEGER         DEFAULT 0,
    message         TEXT,
    started_at      TIMESTAMPTZ     DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

-- =============================================================================
-- 17. SITE PERFORMANCE
-- =============================================================================

CREATE TABLE IF NOT EXISTS site_performance (
    id                      BIGSERIAL       PRIMARY KEY,
    date                    DATE            NOT NULL,
    brand_id                VARCHAR(64)     NOT NULL,
    device                  VARCHAR(16)     NOT NULL CHECK (device IN ('mobile','desktop')),
    performance_score       NUMERIC(5,2),
    lcp_ms                  NUMERIC(10,2),
    cls_score               NUMERIC(8,4),
    fcp_ms                  NUMERIC(10,2),
    ttfb_ms                 NUMERIC(10,2),
    alert_triggered         BOOLEAN         DEFAULT FALSE,
    fetched_at              TIMESTAMPTZ     DEFAULT NOW(),
    CONSTRAINT site_performance_unique UNIQUE (date, brand_id, device)
);

-- =============================================================================
-- 18. SEARCH CONSOLE METRICS
-- =============================================================================

CREATE TABLE IF NOT EXISTS search_console_metrics (
    id                  BIGSERIAL       PRIMARY KEY,
    date                DATE            NOT NULL,
    brand_id            VARCHAR(64)     NOT NULL,
    total_clicks        BIGINT          DEFAULT 0,
    total_impressions   BIGINT          DEFAULT 0,
    average_ctr         NUMERIC(8,4),
    average_position    NUMERIC(8,2),
    top_queries_json    JSONB,
    fetched_at          TIMESTAMPTZ     DEFAULT NOW(),
    CONSTRAINT search_console_unique UNIQUE (date, brand_id)
);

-- =============================================================================
-- 19. CUSTOMER SERVICE TICKETS
-- =============================================================================

CREATE TABLE IF NOT EXISTS customer_service_tickets (
    ticket_id           BIGSERIAL       PRIMARY KEY,
    brand_id            VARCHAR(64)     NOT NULL,
    order_id            VARCHAR(64)     REFERENCES orders(order_id) ON DELETE SET NULL,
    customer_name       VARCHAR(256),
    customer_email      VARCHAR(256),
    date_received       DATE            NOT NULL DEFAULT CURRENT_DATE,
    date_resolved       DATE,
    channel             VARCHAR(32)     CHECK (channel IN ('email','instagram','facebook','other')),
    category            VARCHAR(64)
                            CHECK (category IN (
                                'order_status','return_request','wrong_item',
                                'damaged_item','delivery_issue','product_question',
                                'complaint','compliment','other'
                            )),
    priority            VARCHAR(16)     DEFAULT 'normal'
                            CHECK (priority IN ('low','normal','high','urgent')),
    status              VARCHAR(32)     DEFAULT 'open'
                            CHECK (status IN ('open','in_progress','waiting_customer','resolved','closed')),
    description         TEXT,
    resolution          TEXT,
    assigned_to         VARCHAR(128)    DEFAULT 'wife',
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);

-- =============================================================================
-- 20. LIVE SNAPSHOT (hourly mobile dashboard)
-- =============================================================================

CREATE TABLE IF NOT EXISTS live_snapshot (
    snapshot_at         TIMESTAMPTZ     PRIMARY KEY,
    brand_id            VARCHAR(64)     NOT NULL,
    revenue_today       NUMERIC(10,4)   DEFAULT 0,
    orders_today        INTEGER         DEFAULT 0,
    revenue_last_hour   NUMERIC(10,4)   DEFAULT 0,
    orders_last_hour    INTEGER         DEFAULT 0,
    ad_spend_today      NUMERIC(10,4)   DEFAULT 0,
    ad_spend_meta       NUMERIC(10,4)   DEFAULT 0,
    ad_spend_google     NUMERIC(10,4)   DEFAULT 0,
    mer_today           NUMERIC(8,4),
    expires_at          TIMESTAMPTZ
);

COMMENT ON TABLE live_snapshot IS 'Hourly live data for mobile dashboard. 48hr retention. MER is approximate — Meta data has 15-60 min lag.';

-- =============================================================================
-- 21. TIME TRACKING
-- =============================================================================

CREATE TABLE IF NOT EXISTS time_tracking (
    id                  BIGSERIAL       PRIMARY KEY,
    brand_id            VARCHAR(64)     NOT NULL,
    date                DATE            NOT NULL DEFAULT CURRENT_DATE,
    task_name           VARCHAR(256)    NOT NULL,
    category            VARCHAR(64)
                            CHECK (category IN (
                                'admin','marketing','customer_service','design',
                                'fulfilment','finance','tech','other'
                            )),
    minutes_spent       INTEGER         NOT NULL,
    could_automate      BOOLEAN         DEFAULT FALSE,
    could_outsource     BOOLEAN         DEFAULT FALSE,
    notes               TEXT,
    logged_by           VARCHAR(128),
    created_at          TIMESTAMPTZ     DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_time_tracking_brand_date ON time_tracking (brand_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_time_tracking_category   ON time_tracking (category, brand_id);

COMMENT ON TABLE time_tracking IS 'Time spent on business tasks. Flags for automation and outsourcing opportunities. Metabase shows daily total hours and breakdown by category.';

-- =============================================================================
-- VIEW: ORDER FULFILMENT STATUS
-- =============================================================================

CREATE OR REPLACE VIEW order_fulfilment_status AS
SELECT
    o.order_name,
    o.brand_id,
    o.created_at::date                          AS order_date,
    o.revenue_gbp,
    o.cogs_gbp,
    o.financial_status,
    o.refund_amount_gbp,
    o.payment_gateway,
    o.shipping_country_code,
    o.shipping_country_name,
    f.provider,
    f.provider_order_id,
    f.fulfilment_status,
    f.is_cancelled,
    f.tracking_number,
    f.tracking_url,
    f.carrier,
    f.dispatched_at,
    f.estimated_delivery_at,
    f.delivered_at,
    f.hours_to_dispatch,
    f.hours_to_delivery,
    f.fulfillment_country,
    CASE
        WHEN f.dispatched_at IS NOT NULL AND f.delivered_at IS NULL
        THEN EXTRACT(DAY FROM NOW() - f.dispatched_at)::INTEGER
        ELSE NULL
    END                                         AS days_since_dispatch,
    7                                           AS delivery_threshold_days,
    CASE
        WHEN f.is_cancelled = TRUE              THEN FALSE
        WHEN f.fulfilment_status IN (
            'returned','not_connected',
            'canceled','cancelled')             THEN FALSE
        WHEN f.delivered_at IS NOT NULL         THEN FALSE
        WHEN f.fulfilment_status IN (
            'fulfilled','shipment_delivered')   THEN FALSE
        WHEN f.dispatched_at IS NULL            THEN FALSE
        ELSE EXTRACT(DAY FROM NOW() - f.dispatched_at)::INTEGER > 7
    END                                         AS is_late,
    CASE
        WHEN f.is_cancelled = TRUE              THEN 'cancelled'
        WHEN f.fulfilment_status = 'returned'   THEN 'returned'
        WHEN f.fulfilment_status = 'returned_resolved' THEN 'returned_resolved'
        WHEN f.fulfilment_status = 'not_connected' THEN 'not_connected'
        WHEN f.delivered_at IS NOT NULL         THEN 'delivered'
        WHEN f.fulfilment_status IN (
            'fulfilled','shipment_delivered')   THEN 'delivered'
        WHEN f.dispatched_at IS NOT NULL        THEN 'in_transit'
        WHEN f.fulfilment_status = 'in_production' THEN 'in_production'
        WHEN f.fulfilment_status = 'printed'    THEN 'printed'
        WHEN f.fulfilment_status = 'passed'     THEN 'passed'
        WHEN f.fulfilment_status = 'pending_approval' THEN 'pending_approval'
        WHEN f.fulfilment_status = 'sent_to_production' THEN 'in_production'
        WHEN o.fulfillment_match_status = 'unmatched' THEN 'unmatched'
        ELSE COALESCE(f.fulfilment_status, 'unknown')
    END                                         AS status_summary,
    o.fulfillment_match_status,
    o.fulfillment_provider,
    o.fulfillment_order_id,
    f.dispatch_alert_sent,
    f.delivery_alert_sent,
    f.override_flag
FROM orders o
LEFT JOIN fulfilments f ON f.shopify_order_id = o.order_id
WHERE o.brand_id = 'your_brand_id'
ORDER BY o.created_at DESC;

COMMENT ON VIEW order_fulfilment_status IS
'Unified fulfilment + financial status. Provider agnostic. 7 day threshold. Includes financial_status and refund_amount_gbp from orders.';

-- =============================================================================
-- CONFIRMATION
-- =============================================================================

DO $$
BEGIN
    RAISE NOTICE '==============================================';
    RAISE NOTICE 'Your Brand schema v7.0 complete';
    RAISE NOTICE 'Tables: 21 + 1 view';
    RAISE NOTICE '';
    RAISE NOTICE 'v7.0 changes:';
    RAISE NOTICE '  - orders: financial_status + refund fields added';
    RAISE NOTICE '  - returns: redesigned — Shopify as financial source of truth';
    RAISE NOTICE '  - time_tracking: new table (table 21)';
    RAISE NOTICE '  - order_fulfilment_status: includes financial_status';
    RAISE NOTICE '';
    RAISE NOTICE 'Refund data (all historical orders):';
    RAISE NOTICE '  paid:               13,727 orders';
    RAISE NOTICE '  partially_refunded:    117 orders — £1,953 refunded';
    RAISE NOTICE '  refunded:               84 orders — £2,698 refunded';
    RAISE NOTICE '  partially_paid:          6 orders';
    RAISE NOTICE '  Total customer refunds: £6,561 across 201 orders';
    RAISE NOTICE '==============================================';
END $$;
