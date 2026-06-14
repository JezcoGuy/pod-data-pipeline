-- =============================================================================
-- Migration v8.5: Customer subscription tracking + Klaviyo forms table
-- =============================================================================
-- Two changes, related but independent:
--
--   1. customers gets six new columns capturing email + SMS marketing consent
--      state from Shopify. The existing customers_sync.py incremental flow
--      (updated_at_min) automatically picks up unsubscribe events when
--      Shopify updates the customer record. No polling needed.
--
--   2. klaviyo_forms_daily new table for sign-up form metrics (views,
--      submits, revenue). Campaigns + flows write to existing
--      email_campaigns table; forms get their own grain.
--
-- Idempotent ALTERs / CREATE IF NOT EXISTS. Re-applying is safe.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. EXTEND customers with marketing consent state
-- =============================================================================
-- Shopify's customer object exposes a structured marketing consent block per
-- channel (email + SMS). State enum: 'subscribed', 'unsubscribed',
-- 'not_subscribed', 'pending', 'invalid', 'redacted'.
--
-- consent_updated_at is the timestamp of the last state change — the
-- effective "unsubscribed_at" for unsubscribe events.

ALTER TABLE customers
    ADD COLUMN IF NOT EXISTS email_marketing_state         VARCHAR(32),
    ADD COLUMN IF NOT EXISTS email_marketing_opt_in_level  VARCHAR(32),
    ADD COLUMN IF NOT EXISTS email_consent_updated_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS sms_marketing_state           VARCHAR(32),
    ADD COLUMN IF NOT EXISTS sms_marketing_opt_in_level    VARCHAR(32),
    ADD COLUMN IF NOT EXISTS sms_consent_updated_at        TIMESTAMPTZ;

-- Index for "find recent unsubscribes" and "active subscribers" queries
CREATE INDEX IF NOT EXISTS idx_customers_email_state
    ON customers (email_marketing_state, brand_id)
    WHERE email_marketing_state IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customers_email_consent_updated
    ON customers (email_consent_updated_at DESC NULLS LAST, brand_id)
    WHERE email_consent_updated_at IS NOT NULL;

COMMENT ON COLUMN customers.email_marketing_state IS
    'Shopify email_marketing_consent.state. subscribed/unsubscribed/not_subscribed/pending/invalid/redacted.';
COMMENT ON COLUMN customers.email_consent_updated_at IS
    'Shopify email_marketing_consent.consent_updated_at. Effective unsubscribe timestamp for state=unsubscribed.';


-- =============================================================================
-- 2. CREATE klaviyo_forms_daily — sign-up form metrics
-- =============================================================================
-- Grain: one row per (date, form_id). Populated by klaviyo_sync.py via the
-- Klaviyo Forms Values Report API.

CREATE TABLE IF NOT EXISTS klaviyo_forms_daily (
    id                  BIGSERIAL       PRIMARY KEY,
    date                DATE            NOT NULL,
    brand_id            VARCHAR(64)     NOT NULL,

    form_id             VARCHAR(128)    NOT NULL,
    form_name           VARCHAR(512),

    -- Funnel
    views               BIGINT          DEFAULT 0,
    submits             BIGINT          DEFAULT 0,
    qualified_submits   BIGINT          DEFAULT 0,
                            -- new emails only (excludes already-known profiles)

    submit_rate         NUMERIC(8,4)
        GENERATED ALWAYS AS (
            CASE WHEN views > 0
                 THEN submits::numeric / views
                 ELSE NULL END
        ) STORED,

    -- Revenue attributed to this form's sign-ups (per Klaviyo's attribution window)
    revenue_attributed  NUMERIC(10,4)   DEFAULT 0,

    synced_at           TIMESTAMPTZ,

    CONSTRAINT klaviyo_forms_daily_unique UNIQUE (date, brand_id, form_id)
);

CREATE INDEX IF NOT EXISTS idx_klaviyo_forms_date  ON klaviyo_forms_daily (date, brand_id);
CREATE INDEX IF NOT EXISTS idx_klaviyo_forms_form  ON klaviyo_forms_daily (form_id, brand_id, date DESC);

COMMENT ON TABLE klaviyo_forms_daily IS
    'Daily Klaviyo sign-up form metrics: views, submits, qualified submits (new emails), revenue. Populated by klaviyo_sync.py.';


COMMIT;

-- =============================================================================
-- Verify (run after applying)
-- =============================================================================
-- \d customers
-- \d klaviyo_forms_daily
