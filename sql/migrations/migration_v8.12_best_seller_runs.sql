-- Migration v8.12 — Best seller sync run history
-- ------------------------------------------------
-- Captures one row per successful --execute run of best_seller_sync.py.
-- Read by nightly_alert.check_best_seller_sync() to build the daily email block.
--
-- Why a table (not a flat file): nightly_alert is DB-driven by design.
-- Every check_* there queries Postgres directly — keeping that pattern
-- consistent here.

CREATE TABLE IF NOT EXISTS best_seller_sync_runs (
    id                     BIGSERIAL PRIMARY KEY,
    run_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    brand_id               TEXT        NOT NULL,
    mode                   TEXT        NOT NULL DEFAULT 'execute',
    total_qualifying       INTEGER     NOT NULL,
    total_currently_tagged INTEGER     NOT NULL,        -- state AFTER sync
    adds_count             INTEGER     NOT NULL DEFAULT 0,
    removes_count          INTEGER     NOT NULL DEFAULT 0,
    keeps_count            INTEGER     NOT NULL DEFAULT 0,
    added_products         JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- [{product_id, design}, ...]
    removed_products       JSONB       NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_best_seller_sync_runs_brand_at
    ON best_seller_sync_runs (brand_id, run_at DESC);
