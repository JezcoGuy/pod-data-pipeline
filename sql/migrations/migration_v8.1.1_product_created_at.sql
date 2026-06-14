-- =============================================================================
-- Migration v8.1.1: product_created_at on product_catalogue
-- =============================================================================
-- One-column addition. Enables filtering catalogue testing reports to
-- recently-created designs (the natural scope for "testing new products"
-- — hero products being scaled via statics shouldn't appear in that view).
--
-- Populated by shopify_products_sync.py from Shopify's product.created_at.
-- =============================================================================

BEGIN;

ALTER TABLE product_catalogue
    ADD COLUMN IF NOT EXISTS product_created_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_product_catalogue_created_at
    ON product_catalogue (product_created_at)
    WHERE product_created_at IS NOT NULL;

COMMENT ON COLUMN product_catalogue.product_created_at IS
    'Shopify product.created_at. Used to filter catalogue testing reports to recently-created designs.';

COMMIT;
