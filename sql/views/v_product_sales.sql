-- View: v_product_sales
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_product_sales.sql

CREATE OR REPLACE VIEW v_product_sales AS
 SELECT brand_id,
    product_id,
    max(product_handle::text) AS product_handle,
    max(product_title::text) AS product_title,
    max(product_type::text) AS product_type,
    max(product_created_at) AS product_created_at,
    bool_or(variant_active) AS any_variant_active,
    count(DISTINCT order_id) AS orders,
    count(DISTINCT variant_id) AS variants_sold,
    sum(quantity) AS units_sold,
    round(sum(line_total_gbp), 4) AS revenue_gbp,
    round(sum(COALESCE(line_cogs_gbp, 0::numeric)), 4) AS cogs_gbp,
    round(sum(line_gross_profit_gbp), 4) AS gross_profit_gbp,
    min(order_created_at) AS first_sold_at,
    max(order_created_at) AS last_sold_at
   FROM v_variant_sales
  GROUP BY brand_id, product_id;
