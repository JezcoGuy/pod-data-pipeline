-- Migration v8.13 — Shopify Payments payout ingestion
-- ----------------------------------------------------
-- Two new tables (one per payout, one per transaction line) plus two views
-- that derive from them.
--
-- Why: orders.shopify_fee_gbp / shopify_fee_pct were never populated because
-- the REST orders endpoint doesn't expose per-order fees. They live in the
-- Shopify Payments payout-line response. This migration lands the staging
-- tables; shopify_payouts_sync.py populates them and then back-fills the
-- two columns on orders.

-- ─── shopify_payouts ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shopify_payouts (
    payout_id              BIGINT PRIMARY KEY,
    brand_id               VARCHAR     NOT NULL,
    status                 VARCHAR,
    payout_date            DATE,
    currency               VARCHAR(3),
    amount_gbp             NUMERIC(10,2),
    charges_gross          NUMERIC(10,2),
    charges_fees           NUMERIC(10,2),
    refunds_gross          NUMERIC(10,2),
    refunds_fees           NUMERIC(10,2),
    adjustments_gross      NUMERIC(10,2),
    adjustments_fees       NUMERIC(10,2),
    reserved_funds_gross   NUMERIC(10,2),
    synced_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shopify_payouts_brand_date
    ON shopify_payouts (brand_id, payout_date DESC);

-- ─── shopify_payout_lines ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shopify_payout_lines (
    line_id           BIGINT PRIMARY KEY,
    payout_id         BIGINT REFERENCES shopify_payouts(payout_id),
    brand_id          VARCHAR     NOT NULL,
    type              VARCHAR,
    source_type       VARCHAR,
    source_order_id   BIGINT,
    order_name        VARCHAR,
    currency          VARCHAR(3),
    amount            NUMERIC(10,2),
    fee               NUMERIC(10,2),
    net               NUMERIC(10,2),
    processed_at      TIMESTAMPTZ,
    synced_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payout_lines_order_id
    ON shopify_payout_lines (source_order_id);
CREATE INDEX IF NOT EXISTS idx_payout_lines_payout_id
    ON shopify_payout_lines (payout_id);
CREATE INDEX IF NOT EXISTS idx_payout_lines_processed_at
    ON shopify_payout_lines (processed_at);

-- ─── Summary views ───────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_shopify_payout_summary AS
SELECT
    DATE_TRUNC('month', payout_date)                                                AS month,
    COUNT(DISTINCT payout_id)                                                       AS payouts,
    ROUND(SUM(amount_gbp)::numeric, 2)                                              AS net_paid_to_bank,
    ROUND(SUM(charges_gross)::numeric, 2)                                           AS gross_sales,
    ROUND(SUM(charges_fees)::numeric, 2)                                            AS total_shopify_fees,
    ROUND(SUM(refunds_gross)::numeric, 2)                                           AS total_refunds,
    ROUND(SUM(adjustments_gross)::numeric, 2)                                       AS total_adjustments,
    ROUND((SUM(charges_fees) / NULLIF(SUM(charges_gross), 0) * 100)::numeric, 3)    AS effective_fee_pct
FROM shopify_payouts
WHERE brand_id = 'your_brand_id'
  AND status = 'paid'
GROUP BY DATE_TRUNC('month', payout_date)
ORDER BY month DESC;

CREATE OR REPLACE VIEW v_fee_by_payment_gateway AS
SELECT
    o.payment_gateway,
    COUNT(DISTINCT o.order_id)                                                      AS orders,
    ROUND(SUM(o.revenue_gbp)::numeric, 2)                                           AS gross_revenue,
    ROUND(SUM(o.shopify_fee_gbp)::numeric, 2)                                       AS total_fees,
    ROUND(AVG(o.shopify_fee_pct)::numeric, 4)                                       AS avg_fee_pct,
    ROUND(AVG(o.revenue_gbp)::numeric, 2)                                           AS avg_order_value
FROM orders o
WHERE o.brand_id = 'your_brand_id'
  AND o.financial_status NOT IN ('voided', 'refunded')
  AND o.shopify_fee_gbp IS NOT NULL
  AND o.payment_gateway IS NOT NULL
  AND o.payment_gateway != ''
GROUP BY o.payment_gateway
ORDER BY total_fees DESC;
