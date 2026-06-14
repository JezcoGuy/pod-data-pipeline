-- =============================================================================
-- Migration v8.6: live_snapshot table for hourly MER dashboard
-- =============================================================================
-- One row per (brand, local_date, local_hour). Populated by live_sync.py
-- hourly. Gross figures only — refunds handled in nightly P&L reporting,
-- not in the live dashboard.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS live_snapshot (
    id                          BIGSERIAL       PRIMARY KEY,
    snapshot_at                 TIMESTAMPTZ     NOT NULL,
    snapshot_date               DATE            NOT NULL,
                                -- local Europe/London date the snapshot describes
    snapshot_hour               INTEGER         NOT NULL CHECK (snapshot_hour BETWEEN 0 AND 23),
                                -- hour of day in Europe/London
    brand_id                    VARCHAR(64)     NOT NULL,

    -- Shopify running totals for the local-today window
    shopify_orders_today        INTEGER         DEFAULT 0,
    shopify_items_today         INTEGER         DEFAULT 0,
                                -- sum of line_items.quantity across today's orders
    shopify_revenue_today_gbp   NUMERIC(10,4)   DEFAULT 0,
                                -- gross (total_price), not net of refunds

    shopify_aov_today_gbp       NUMERIC(10,4)
        GENERATED ALWAYS AS (
            CASE WHEN shopify_orders_today > 0
                 THEN shopify_revenue_today_gbp / shopify_orders_today
                 ELSE NULL END
        ) STORED,

    -- Meta running totals for the local-today window
    meta_spend_today_gbp        NUMERIC(10,4)   DEFAULT 0,
    meta_impressions_today      BIGINT          DEFAULT 0,
    meta_clicks_today           BIGINT          DEFAULT 0,

    -- The headline number — gross revenue / meta spend
    mer                         NUMERIC(10,4)
        GENERATED ALWAYS AS (
            CASE WHEN meta_spend_today_gbp > 0
                 THEN shopify_revenue_today_gbp / meta_spend_today_gbp
                 ELSE NULL END
        ) STORED,

    synced_at                   TIMESTAMPTZ,

    CONSTRAINT live_snapshot_unique UNIQUE (brand_id, snapshot_date, snapshot_hour)
);

CREATE INDEX IF NOT EXISTS idx_live_snapshot_date
    ON live_snapshot (snapshot_date DESC, brand_id);
CREATE INDEX IF NOT EXISTS idx_live_snapshot_at
    ON live_snapshot (snapshot_at DESC);

COMMENT ON TABLE live_snapshot IS
    'Hourly running totals for today: orders, items, gross revenue (Shopify) + spend, impressions, clicks (Meta). Headline metric is gross MER. Refunds intentionally excluded — see nightly P&L for refund-adjusted views.';

COMMIT;
