-- =============================================================================
-- Migration v8.8: Parse Shopify item_id components on ga4_products_daily
-- =============================================================================
-- GA4's item_id from the Shopify-native integration is a compound string
-- like 'shopify_<store>_<product_id>_<variant_id>'. Currently stored raw.
-- This migration adds two parsed columns and backfills existing rows so
-- ga4_products_daily can join cleanly to product_catalogue.product_id
-- without inline SUBSTRING / regex on every query.
--
-- ga4_sync.py is updated in tandem so new rows populate these columns at
-- write time.
-- =============================================================================

BEGIN;

ALTER TABLE ga4_products_daily
    ADD COLUMN IF NOT EXISTS shopify_product_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS shopify_variant_id VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_ga4_products_shopify_product
    ON ga4_products_daily (shopify_product_id, brand_id)
    WHERE shopify_product_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ga4_products_shopify_variant
    ON ga4_products_daily (shopify_variant_id, brand_id)
    WHERE shopify_variant_id IS NOT NULL;

-- Backfill: extract the last two _-separated numeric groups from item_ids
-- that match the 'shopify_<store>_<digits>_<digits>' pattern. Non-matching
-- rows (sentinel '(not set)', non-Shopify products, malformed) stay NULL.
UPDATE ga4_products_daily
SET
    shopify_product_id = (regexp_match(item_id, '^shopify_[^_]+_(\d+)_(\d+)$'))[1],
    shopify_variant_id = (regexp_match(item_id, '^shopify_[^_]+_(\d+)_(\d+)$'))[2]
WHERE item_id ~ '^shopify_[^_]+_\d+_\d+$';

COMMENT ON COLUMN ga4_products_daily.shopify_product_id IS
    'Parent product_id parsed from item_id (shopify_<store>_<product>_<variant>). Joins to product_catalogue.product_id.';
COMMENT ON COLUMN ga4_products_daily.shopify_variant_id IS
    'Variant_id parsed from item_id. Joins to product_catalogue.variant_id.';

COMMIT;
