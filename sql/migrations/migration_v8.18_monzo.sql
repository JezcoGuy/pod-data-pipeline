-- Migration v8.18 — Monzo Business transaction ingestion
-- --------------------------------------------------------
-- Two tables and two views, plus a seeded pattern set on the category map.
-- monzo_transactions holds one row per Monzo transaction with both Monzo's
-- own classification and our own (`our_category`). monzo_category_map
-- holds the rules used to derive `our_category` at ingestion time.
--
-- Amounts in Monzo's API are in MINOR UNITS (pence) — we divide by 100 at
-- ingestion to match the rest of the schema. local_amount/local_currency
-- preserve the original currency for FX card transactions.

CREATE TABLE IF NOT EXISTS monzo_transactions (
    transaction_id        VARCHAR(64) PRIMARY KEY,
    brand_id              VARCHAR(64) NOT NULL,
    account_id            VARCHAR(64) NOT NULL,
    created_at            TIMESTAMPTZ,
    settled_at            TIMESTAMPTZ,
    description           VARCHAR(512),
    amount_gbp            NUMERIC(12,2),     -- positive=in, negative=out
    currency              VARCHAR(3),
    local_amount_gbp      NUMERIC(12,2),     -- original-currency amount, divided by 100
    local_currency        VARCHAR(3),
    monzo_category        VARCHAR(64),       -- Monzo's auto-category
    scheme                VARCHAR(64),       -- payport_faster_payments / mastercard / monzo_business_account_billing / ...
    counterparty_name     VARCHAR(255),
    counterparty_account  VARCHAR(64),
    counterparty_sort     VARCHAR(16),
    merchant_id           VARCHAR(64),
    merchant_name         VARCHAR(255),
    merchant_category     VARCHAR(64),
    notes                 VARCHAR(1024),
    is_load               BOOLEAN,
    include_in_spending   BOOLEAN,
    our_category          VARCHAR(64),
    our_category_source   VARCHAR(16) DEFAULT 'auto',
    needs_review          BOOLEAN     DEFAULT FALSE,
    raw_payload           JSONB,
    synced_at             TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_monzo_created_at
    ON monzo_transactions (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_monzo_our_category
    ON monzo_transactions (our_category);
CREATE INDEX IF NOT EXISTS idx_monzo_needs_review
    ON monzo_transactions (needs_review) WHERE needs_review = TRUE;
CREATE INDEX IF NOT EXISTS idx_monzo_amount
    ON monzo_transactions (amount_gbp);


CREATE TABLE IF NOT EXISTS monzo_category_map (
    id              SERIAL PRIMARY KEY,
    pattern         VARCHAR(255) NOT NULL,
    match_field     VARCHAR(32)  NOT NULL DEFAULT 'description',  -- description / counterparty_name / merchant_name
    match_type      VARCHAR(16)  NOT NULL DEFAULT 'ilike',         -- ilike / exact / starts_with
    our_category    VARCHAR(64)  NOT NULL,
    category_label  VARCHAR(128),
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_category_map_pattern
    ON monzo_category_map (pattern, match_field);


-- Seeded categorisation rules (from brief Section 7). Idempotent.
INSERT INTO monzo_category_map (pattern, match_field, match_type, our_category, category_label) VALUES
  ('SHOPIFY YOUR_BRAND_NAME',  'description',       'exact', 'INCOME_SHOPIFY',     'Shopify Payout'),
  ('Stripe Payments UK', 'counterparty_name', 'ilike', 'INCOME_SHOPIFY',     'Shopify Payout'),
  ('KLARNA BANK AB',     'counterparty_name', 'ilike', 'INCOME_KLARNA',      'Klarna Settlement'),
  ('PAYPAL',             'counterparty_name', 'ilike', 'INCOME_PAYPAL',      'PayPal Payout'),
  ('AMERICAN EXP',       'counterparty_name', 'ilike', 'AMEX_PAYMENT',       'Amex Card Payment'),
  ('Xero',               'merchant_name',     'ilike', 'OVERHEAD_ACCOUNTING','Xero Subscription'),
  ('Monzo Business',     'description',       'ilike', 'OVERHEAD_BANKING',   'Monzo Business Fee'),
  ('Google Play',        'merchant_name',     'ilike', 'OVERHEAD_SUBS',      'Google Subscription'),
  ('Photopea',           'merchant_name',     'ilike', 'OVERHEAD_SUBS',      'Photopea Subscription'),
  ('Klaviyo',            'merchant_name',     'ilike', 'OVERHEAD_EMAIL',     'Klaviyo Subscription'),
  ('Shopify',            'merchant_name',     'ilike', 'OVERHEAD_PLATFORM',  'Shopify Subscription'),
  ('GUY MULRY',          'counterparty_name', 'ilike', 'DRAWINGS',           'Director Drawings'),
  ('SONA',               'counterparty_name', 'ilike', 'DRAWINGS',           'Director Drawings'),
  ('Tomorrowcreative',   'counterparty_name', 'ilike', 'SUPPLIER_CREATIVE',  'Creative Services'),
  ('Facebook',           'merchant_name',     'ilike', 'ADS_META',           'Meta Advertising'),
  ('Meta',               'merchant_name',     'ilike', 'ADS_META',           'Meta Advertising')
ON CONFLICT (pattern, match_field) DO NOTHING;


CREATE OR REPLACE VIEW v_monzo_monthly AS
SELECT
    DATE_TRUNC('month', created_at)                                                            AS month,
    our_category,
    COUNT(*)                                                                                   AS transactions,
    ROUND(SUM(amount_gbp)::numeric, 2)                                                         AS total_gbp,
    ROUND(SUM(CASE WHEN amount_gbp > 0 THEN amount_gbp ELSE 0 END)::numeric, 2)                AS total_in,
    ROUND(SUM(CASE WHEN amount_gbp < 0 THEN amount_gbp ELSE 0 END)::numeric, 2)                AS total_out
FROM monzo_transactions
WHERE brand_id = 'your_brand_id'
  AND our_category IS NOT NULL
GROUP BY DATE_TRUNC('month', created_at), our_category
ORDER BY month DESC, total_gbp DESC;


CREATE OR REPLACE VIEW v_monzo_needs_review AS
SELECT
    transaction_id,
    created_at::date AS date,
    description,
    counterparty_name,
    merchant_name,
    ROUND(amount_gbp::numeric, 2) AS amount_gbp,
    monzo_category,
    notes
FROM monzo_transactions
WHERE needs_review = TRUE
  AND brand_id = 'your_brand_id'
ORDER BY created_at DESC;
