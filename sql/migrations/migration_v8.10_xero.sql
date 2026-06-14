-- =============================================================================
-- Migration v8.10: Xero account balance daily snapshots
-- =============================================================================
-- v1 of Xero integration. Captures bank + credit card account balances
-- daily so we have a time-series of cash-on-hand. Transactions and
-- invoices can be added in a follow-up migration when needed for full
-- reconciliation.
--
-- Populated by scripts/xero_sync.py via Xero's Accounting API.
-- Auth uses OAuth2 refresh tokens stored in /opt/your_brand_id/credentials/.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS xero_account_balances_daily (
    id                  BIGSERIAL       PRIMARY KEY,
    date                DATE            NOT NULL,
    brand_id            VARCHAR(64)     NOT NULL,

    xero_tenant_id      VARCHAR(64)     NOT NULL,
                              -- Xero organisation UUID; one Your Brand =
                              -- one tenant typically, but multi-tenant
                              -- ready
    xero_account_id     VARCHAR(64)     NOT NULL,
                              -- Xero account UUID
    account_name        VARCHAR(256),
    account_code        VARCHAR(64),
                              -- Xero chart-of-accounts code (e.g. '090')
    account_type        VARCHAR(32),
                              -- BANK / CREDITCARD / PAYPAL / OTHER
    bank_account_type   VARCHAR(32),
                              -- Xero sub-type for banks (CHEQUE / SAVINGS / etc)
    currency_code       VARCHAR(8),

    balance             NUMERIC(14,4),
                              -- Current balance at fetch time
    ytd_balance         NUMERIC(14,4),
                              -- Year-to-date if exposed; nullable

    fetched_at          TIMESTAMPTZ,
                              -- When Xero last updated this account snapshot
    synced_at           TIMESTAMPTZ,

    CONSTRAINT xero_account_balances_unique
        UNIQUE (date, brand_id, xero_tenant_id, xero_account_id)
);

CREATE INDEX IF NOT EXISTS idx_xero_balances_date
    ON xero_account_balances_daily (date DESC, brand_id);
CREATE INDEX IF NOT EXISTS idx_xero_balances_account
    ON xero_account_balances_daily (xero_account_id, date DESC);
CREATE INDEX IF NOT EXISTS idx_xero_balances_type
    ON xero_account_balances_daily (account_type, brand_id);

COMMENT ON TABLE xero_account_balances_daily IS
    'Daily Xero bank/credit account balance snapshots. One row per (date, account). Source for cash-on-hand dashboards and P&L overlays. Transactions + invoices live in separate tables (TBD).';

COMMIT;
