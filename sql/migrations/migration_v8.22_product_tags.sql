-- Migration v8.22 — product_tags column on product_catalogue
-- ------------------------------------------------------------
-- Shopify's product API already returns a comma-joined `tags` string per
-- product. Storing it lets us answer questions like "which products carry
-- the 'your_my' tag" without round-tripping to the Shopify API. Comma-
-- joined string preserves Shopify's natural format so ILIKE '%tag%'
-- queries work cleanly.

ALTER TABLE product_catalogue
    ADD COLUMN IF NOT EXISTS product_tags TEXT;

CREATE INDEX IF NOT EXISTS idx_product_catalogue_tags
    ON product_catalogue (product_tags)
    WHERE product_tags IS NOT NULL;
