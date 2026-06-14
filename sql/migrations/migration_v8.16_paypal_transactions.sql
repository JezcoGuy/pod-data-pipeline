-- Migration v8.16 — PayPal Transactions Search ingestion
-- --------------------------------------------------------
-- One row per PayPal transaction (sale, currency conversion, refund, etc).
-- The join key is `transaction_id`, which matches
-- shopify_order_transactions.authorization for any PayPal-paid Shopify order.
--
-- shopify_order_id is denormalised onto each row but re-resolved on every
-- sync run so that PayPal rows ingested BEFORE their matching bridge-table
-- row exists get back-filled later (the order-transactions bridge is being
-- back-filled incrementally — see project notes).

CREATE TABLE IF NOT EXISTS paypal_transactions (
    transaction_id         VARCHAR(64) PRIMARY KEY,
    brand_id               VARCHAR(64) NOT NULL,
    paypal_reference_id    VARCHAR(64),
    paypal_reference_type  VARCHAR(16),
    shopify_order_id       VARCHAR(64),       -- resolved via bridge; NULL until matched
    order_name             VARCHAR(64),       -- resolved via bridge; NULL until matched
    transaction_type       VARCHAR(8),        -- sourced from transaction_event_code (e.g. T0006)
    transaction_status     VARCHAR(8),        -- S=success, V=reversal, P=pending, D=denied
    transaction_amount     NUMERIC(10,2),     -- gross, signed (refunds are negative)
    transaction_currency   VARCHAR(3),
    paypal_fee             NUMERIC(10,2),     -- abs(fee_amount); PayPal returns it negative
    net_amount             NUMERIC(10,2),     -- amount + fee_amount (signed math)
    fee_currency           VARCHAR(3),        -- kept separate for future FX edge cases
    payer_email            VARCHAR(255),
    payer_name             VARCHAR(255),
    transaction_initiated  TIMESTAMPTZ,
    transaction_updated    TIMESTAMPTZ,
    raw_payload            JSONB,
    synced_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paypal_transactions_shopify_order
    ON paypal_transactions (shopify_order_id)
    WHERE shopify_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paypal_transactions_initiated
    ON paypal_transactions (transaction_initiated DESC);
CREATE INDEX IF NOT EXISTS idx_paypal_transactions_status
    ON paypal_transactions (transaction_status);
CREATE INDEX IF NOT EXISTS idx_paypal_transactions_brand_type
    ON paypal_transactions (brand_id, transaction_type);

-- Monthly summary view (matches brief Section 10)
CREATE OR REPLACE VIEW v_paypal_summary AS
SELECT
    DATE_TRUNC('month', transaction_initiated)                                                  AS month,
    COUNT(*) FILTER (WHERE transaction_type = 'T0006')                                          AS sales,
    COUNT(*) FILTER (WHERE transaction_type = 'T1107')                                          AS refunds,
    ROUND(SUM(transaction_amount) FILTER (WHERE transaction_type = 'T0006')::numeric, 2)        AS gross_sales,
    ROUND(SUM(paypal_fee)         FILTER (WHERE transaction_type = 'T0006')::numeric, 2)        AS total_fees,
    ROUND(SUM(net_amount)         FILTER (WHERE transaction_type = 'T0006')::numeric, 2)        AS net_received,
    ROUND(AVG(paypal_fee / NULLIF(transaction_amount, 0) * 100)
        FILTER (WHERE transaction_type = 'T0006' AND transaction_amount > 0)::numeric, 3)       AS avg_fee_pct
FROM paypal_transactions
WHERE brand_id = 'your_brand_id'
  AND transaction_status = 'S'
GROUP BY DATE_TRUNC('month', transaction_initiated)
ORDER BY month DESC;
