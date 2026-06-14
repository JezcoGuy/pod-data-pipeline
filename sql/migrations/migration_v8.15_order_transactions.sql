-- Migration v8.15 — Shopify Order Transactions ingestion
-- --------------------------------------------------------
-- One row per Shopify order transaction (sale, capture, refund, void, etc).
-- The critical column is `authorization_code`: for PayPal transactions this
-- holds PayPal's transaction_id verbatim, giving us a clean join key into
-- the future `paypal_transactions` table.
--
-- The full Shopify `receipt` blob (including the PayPal v2 capture payload
-- when gateway='paypal') is kept as JSONB for forensic access / future use.

CREATE TABLE IF NOT EXISTS shopify_order_transactions (
    transaction_id      BIGINT       PRIMARY KEY,
    order_id            VARCHAR(64)  NOT NULL,
    brand_id            VARCHAR(64)  NOT NULL,
    kind                VARCHAR(32),   -- sale / capture / refund / void / authorization
    gateway             VARCHAR(64),   -- paypal / shopify_payments / manual / gift_card / ...
    status              VARCHAR(32),   -- success / pending / error / failure
    amount              NUMERIC(10,2),
    currency            VARCHAR(3),
    authorization_code  VARCHAR(128),  -- PayPal transaction_id when gateway='paypal'
    processed_at        TIMESTAMPTZ,
    parent_id           BIGINT,        -- for refund/void → references the sale
    source_name         VARCHAR(64),
    receipt             JSONB,
    synced_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sot_order_id
    ON shopify_order_transactions (order_id);
CREATE INDEX IF NOT EXISTS idx_sot_gateway_authcode
    ON shopify_order_transactions (gateway, authorization_code)
    WHERE authorization_code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sot_processed_at
    ON shopify_order_transactions (processed_at DESC);
CREATE INDEX IF NOT EXISTS idx_sot_brand_gateway
    ON shopify_order_transactions (brand_id, gateway);
