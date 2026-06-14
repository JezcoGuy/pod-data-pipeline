-- Migration v8.19 — Amex CSV ingestion
-- --------------------------------------
-- Three tables for ingesting monthly Amex statement CSVs:
--   amex_transactions    — one row per CSV line (synthetic hash PK)
--   amex_category_map    — categorisation rules, scoped by card
--   amex_ingestion_log   — per-file run history so re-running skips done files
--
-- Amex CSVs have NO transaction ID. Dedupe uses sha256(date|description|
-- amount|occurrence_index|card) so identical same-day charges (e.g. three
-- Gelato £200 top-ups in one day) all survive while re-uploading the same
-- file is a safe no-op.

CREATE TABLE IF NOT EXISTS amex_transactions (
    transaction_hash      VARCHAR(64)  PRIMARY KEY,
    brand_id              VARCHAR(64)  NOT NULL,
    card                  VARCHAR(32)  NOT NULL,        -- 'platinum' / 'nectar'
    transaction_date      DATE         NOT NULL,
    description           VARCHAR(512) NOT NULL,        -- raw from CSV
    description_clean     VARCHAR(512),                 -- whitespace-collapsed
    merchant_name         VARCHAR(255),                 -- before first 3+ space run
    amount_gbp            NUMERIC(10,2) NOT NULL,       -- +ve = charge, -ve = payment
    transaction_type      VARCHAR(16),                  -- charge / payment / credit
    our_category          VARCHAR(64),
    our_category_source   VARCHAR(16)  DEFAULT 'auto',
    needs_review          BOOLEAN      DEFAULT FALSE,
    source_file           VARCHAR(255),
    ingested_at           TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_amex_date         ON amex_transactions (transaction_date DESC);
CREATE INDEX IF NOT EXISTS idx_amex_card         ON amex_transactions (card);
CREATE INDEX IF NOT EXISTS idx_amex_category     ON amex_transactions (our_category);
CREATE INDEX IF NOT EXISTS idx_amex_needs_review ON amex_transactions (needs_review) WHERE needs_review = TRUE;


CREATE TABLE IF NOT EXISTS amex_category_map (
    id              SERIAL       PRIMARY KEY,
    pattern         VARCHAR(255) NOT NULL,
    match_field     VARCHAR(32)  NOT NULL DEFAULT 'merchant_name',
    match_type      VARCHAR(16)  NOT NULL DEFAULT 'ilike',
    our_category    VARCHAR(64)  NOT NULL,
    category_label  VARCHAR(128),
    card            VARCHAR(32)  NOT NULL DEFAULT 'both',  -- platinum / nectar / both
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_amex_category_map_key
    ON amex_category_map (pattern, match_field, card);


CREATE TABLE IF NOT EXISTS amex_ingestion_log (
    id             SERIAL       PRIMARY KEY,
    filename       VARCHAR(255) NOT NULL,
    card           VARCHAR(32)  NOT NULL,
    rows_found     INTEGER,
    rows_ingested  INTEGER,
    rows_skipped   INTEGER,
    status         VARCHAR(16)  DEFAULT 'pending',     -- pending / success / failed
    error_message  VARCHAR(2048),
    ingested_at    TIMESTAMPTZ  DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_amex_ingestion_log_file_card
    ON amex_ingestion_log (filename, card);


-- Seed category map (from brief Section 6, confirmed against real CSVs).
INSERT INTO amex_category_map (pattern, match_field, match_type, our_category, category_label, card) VALUES
  ('GELATO',          'merchant_name', 'ilike', 'COGS_FULFILMENT',       'Gelato Wallet Top-up',  'platinum'),
  ('FACEBK',          'merchant_name', 'ilike', 'ADS_META',              'Meta Advertising',      'platinum'),
  ('FACEBOOK',        'merchant_name', 'ilike', 'ADS_META',              'Meta Advertising',      'platinum'),

  ('ANTHROPIC',       'merchant_name', 'ilike', 'OVERHEAD_AI',           'Anthropic / Claude',    'nectar'),
  ('OPENAI',          'merchant_name', 'ilike', 'OVERHEAD_AI',           'OpenAI / ChatGPT',      'nectar'),
  ('CHATGPT',         'merchant_name', 'ilike', 'OVERHEAD_AI',           'OpenAI / ChatGPT',      'nectar'),
  ('IDEOGRAM',        'merchant_name', 'ilike', 'OVERHEAD_AI',           'Ideogram AI',           'nectar'),
  ('KLAVIYO',         'merchant_name', 'ilike', 'OVERHEAD_EMAIL',        'Klaviyo',               'nectar'),
  ('SHOPIFY',         'merchant_name', 'ilike', 'OVERHEAD_PLATFORM',     'Shopify Subscription',  'nectar'),
  ('HETZNER',         'merchant_name', 'ilike', 'OVERHEAD_HOSTING',      'Hetzner VPS',           'nectar'),
  ('GOOGLE',          'merchant_name', 'ilike', 'OVERHEAD_SUBS',         'Google Workspace/One',  'nectar'),
  ('AIRTABLE',        'merchant_name', 'ilike', 'OVERHEAD_SUBS',         'Airtable',              'nectar'),
  ('PADDLE',          'merchant_name', 'ilike', 'OVERHEAD_SUBS',         'Paddle Software',       'nectar'),
  ('FASTMAIL',        'merchant_name', 'ilike', 'OVERHEAD_SUBS',         'Fastmail',              'nectar'),
  ('MICROSOFT',       'merchant_name', 'ilike', 'OVERHEAD_SUBS',         'Microsoft',             'nectar'),
  ('COMPANIES HOUSE', 'merchant_name', 'ilike', 'OVERHEAD_LEGAL',        'Companies House',       'nectar'),
  ('A2X',             'merchant_name', 'ilike', 'OVERHEAD_ACCOUNTING',   'A2X Accounting',        'nectar'),
  ('STARBUCKS',       'merchant_name', 'ilike', 'BUSINESS_MEALS',        'Business Meals',        'nectar'),
  ('FACEBK',          'merchant_name', 'ilike', 'ADS_META',              'Meta Advertising',      'nectar'),

  ('PAYMENT RECEIVED','description',   'ilike', 'AMEX_PAYMENT_RECEIVED', 'Amex Payment',          'both')
ON CONFLICT (pattern, match_field, card) DO NOTHING;
