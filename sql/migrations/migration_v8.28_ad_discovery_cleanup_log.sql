-- =============================================================================
-- Migration v8.28 — ad_discovery_cleanup_log
-- =============================================================================
-- Records each ad_discovery_cleanup.py run row-by-row. One row per
-- (run, product) attempt — failure rows have error populated. The
-- nightly_alert.py summary queries this for the last-24h digest.
-- =============================================================================

CREATE TABLE IF NOT EXISTS ad_discovery_cleanup_log (
  id                    SERIAL PRIMARY KEY,
  brand_id              VARCHAR NOT NULL DEFAULT 'your_brand_id',
  run_at                TIMESTAMPTZ DEFAULT NOW(),
  product_id            VARCHAR NOT NULL,
  product_handle        VARCHAR NOT NULL,
  product_title         VARCHAR,
  age_days              INTEGER,
  action_collection     BOOLEAN DEFAULT FALSE,
  action_tag            BOOLEAN DEFAULT FALSE,
  error                 TEXT,
  created_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ad_discovery_cleanup_run_at
  ON ad_discovery_cleanup_log(run_at DESC);
