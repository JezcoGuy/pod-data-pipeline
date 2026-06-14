-- Migration v8.17 — Klarna Settlements ingestion
-- ------------------------------------------------
-- Two tables mirroring the shopify_payouts / shopify_payout_lines pattern:
--   klarna_payouts        — one row per Klarna payout (weekly aggregates)
--   klarna_transactions   — one row per (capture, type, detailed_type) line;
--                            a single sale produces 3 rows (SALE + 2 FEE rows)
--
-- Join key into the existing pipeline (validated):
--   klarna_transactions.merchant_reference2  ↔
--   shopify_order_transactions.receipt ->> 'payment_id'
--   (gateway='Klarna', kind IN ('sale','capture'))
--
-- All monetary fields are stored as decimal pounds (Klarna API returns minor
-- units / pence; divide by 100 at ingestion to match the rest of the schema).

CREATE TABLE IF NOT EXISTS klarna_payouts (
    payment_reference          VARCHAR(64) PRIMARY KEY,    -- Klarna payout ID, numeric string
    brand_id                   VARCHAR(64) NOT NULL,
    currency_code              VARCHAR(3),
    merchant_id                VARCHAR(64),
    merchant_settlement_type   VARCHAR(32),                -- NET / GROSS
    payout_date                TIMESTAMPTZ,
    -- Totals (all converted to decimal pounds; nulls allowed for safety)
    sale_amount                NUMERIC(10,2),
    fee_amount                 NUMERIC(10,2),
    return_amount              NUMERIC(10,2),
    settlement_amount          NUMERIC(10,2),
    tax_amount                 NUMERIC(10,2),
    commission_amount          NUMERIC(10,2),
    commission_reversal_amount NUMERIC(10,2),
    fee_correction_amount      NUMERIC(10,2),
    holdback_amount            NUMERIC(10,2),
    release_amount             NUMERIC(10,2),
    repay_amount               NUMERIC(10,2),
    reversal_amount            NUMERIC(10,2),
    charge_amount              NUMERIC(10,2),
    credit_amount              NUMERIC(10,2),
    fee_refund_amount          NUMERIC(10,2),
    tax_refund_amount          NUMERIC(10,2),
    deposit_amount             NUMERIC(10,2),
    opening_debt_balance       NUMERIC(10,2),
    closing_debt_balance       NUMERIC(10,2),
    synced_at                  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_klarna_payouts_brand_date
    ON klarna_payouts (brand_id, payout_date DESC);


CREATE TABLE IF NOT EXISTS klarna_transactions (
    capture_id                                      VARCHAR(64) NOT NULL,
    type                                            VARCHAR(32) NOT NULL,     -- SALE / FEE / RETURN / REVERSAL / ...
    detailed_type                                   VARCHAR(64) NOT NULL,     -- PURCHASE / PURCHASE_FEE_PERCENTAGE / ...
    payment_reference                               VARCHAR(64),              -- FK to klarna_payouts
    brand_id                                        VARCHAR(64) NOT NULL,
    merchant_id                                     VARCHAR(64),
    klarna_order_id                                 VARCHAR(64),              -- Klarna's internal UUID
    short_order_id                                  VARCHAR(64),              -- human-readable, e.g. 42W6SNM2-1
    merchant_reference1                             VARCHAR(256),             -- Shopify GID-wrapped payment session
    merchant_reference2                             VARCHAR(64),              -- BARE r... token; the join key
    merchant_capture_reference                      VARCHAR(64),
    amount                                          NUMERIC(10,2),            -- decimal pounds
    currency_code                                   VARCHAR(3),
    vat_amount                                      NUMERIC(10,2),
    vat_rate                                        INTEGER,                  -- Klarna's raw integer (e.g. 2000 = 20.00%)
    purchase_country                                VARCHAR(2),
    shipping_address_country                        VARCHAR(2),
    initial_payment_method_type                     VARCHAR(64),              -- slice_it_by_card / invoice / ...
    initial_payment_method_number_of_installments   INTEGER,
    sale_date                                       TIMESTAMPTZ,
    capture_date                                    TIMESTAMPTZ,
    raw_payload                                     JSONB,
    synced_at                                       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (capture_id, type, detailed_type)
);

CREATE INDEX IF NOT EXISTS idx_klarna_transactions_payment_ref
    ON klarna_transactions (payment_reference);
CREATE INDEX IF NOT EXISTS idx_klarna_transactions_merchant_ref2
    ON klarna_transactions (merchant_reference2)
    WHERE merchant_reference2 IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_klarna_transactions_capture
    ON klarna_transactions (capture_id);
CREATE INDEX IF NOT EXISTS idx_klarna_transactions_brand_type
    ON klarna_transactions (brand_id, type);


-- Monthly Klarna summary view, GBP-only sales to stay honest about fee %.
CREATE OR REPLACE VIEW v_klarna_summary AS
SELECT
    DATE_TRUNC('month', sale_date)                                                      AS month,
    COUNT(DISTINCT capture_id) FILTER (WHERE type = 'SALE')                             AS captures,
    ROUND(SUM(amount) FILTER (WHERE type = 'SALE')::numeric, 2)                         AS gross_sales,
    ROUND(SUM(amount) FILTER (WHERE type = 'FEE')::numeric, 2)                          AS total_fees,
    ROUND((SUM(amount) FILTER (WHERE type = 'FEE')
         / NULLIF(SUM(amount) FILTER (WHERE type = 'SALE'), 0) * 100)::numeric, 3)      AS effective_fee_pct
FROM klarna_transactions
WHERE brand_id = 'your_brand_id'
  AND currency_code = 'GBP'
GROUP BY DATE_TRUNC('month', sale_date)
ORDER BY month DESC;
