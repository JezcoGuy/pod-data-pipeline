-- =============================================================================
-- Migration v8.31 — withdrawal_requests
-- =============================================================================
-- Stores EU consumer withdrawal-of-purchase requests submitted via
-- the public withdrawal form (api.your-domain.com/withdrawal → api.py).
--
-- request_id is generated client-side (UUID) and used as the idempotency
-- key — a retried submission with the same request_id is a no-op.
-- =============================================================================

CREATE TABLE IF NOT EXISTS withdrawal_requests (
  id                  SERIAL PRIMARY KEY,
  brand_id            VARCHAR NOT NULL DEFAULT 'your_brand_id',
  request_id          VARCHAR NOT NULL UNIQUE,
  full_name           VARCHAR NOT NULL,
  email               VARCHAR NOT NULL,
  order_reference     VARCHAR NOT NULL,
  additional_details  TEXT,
  withdrawal_statement TEXT,
  submitted_at        TIMESTAMPTZ NOT NULL,
  submitted_at_local  VARCHAR,
  page_url            VARCHAR,
  ip_address          VARCHAR,
  status              VARCHAR DEFAULT 'received'
    CHECK (status IN ('received','processing','completed','rejected')),
  internal_notes      TEXT,
  created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_withdrawal_requests_email
  ON withdrawal_requests(email);
CREATE INDEX IF NOT EXISTS idx_withdrawal_requests_order
  ON withdrawal_requests(order_reference);
