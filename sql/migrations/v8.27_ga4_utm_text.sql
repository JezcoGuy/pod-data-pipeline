-- =============================================================================
-- Migration v8.27 — widen ga4_channels_daily UTM dimensions to TEXT
-- =============================================================================
-- Malformed UTM strings from Meta ads (the platform sometimes injects very
-- long debug parameters) exceeded the original VARCHAR(128)/VARCHAR(255)
-- caps, causing silent row drops on insert.
--
-- For existing deployments: run this migration once. New deployments
-- pick up the TEXT type from v8.4 directly.
-- =============================================================================

ALTER TABLE ga4_channels_daily
    ALTER COLUMN source   TYPE TEXT,
    ALTER COLUMN medium   TYPE TEXT,
    ALTER COLUMN campaign TYPE TEXT;
