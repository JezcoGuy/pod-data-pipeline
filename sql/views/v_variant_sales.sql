-- View: v_variant_sales
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_variant_sales.sql

CREATE OR REPLACE VIEW v_variant_sales AS
 SELECT li.line_item_id,
    li.order_id,
    li.brand_id,
    o.created_at AS order_created_at,
    o.financial_status,
    li.product_id,
    li.variant_id,
    pc.product_handle,
    li.product_title,
    li.variant_title,
    pc.product_type,
    pc.product_created_at,
    pc.active AS variant_active,
    li.quantity,
    li.unit_price_gbp,
    li.line_total_gbp,
    COALESCE(li.line_cogs_gbp, (li.line_total_gbp / NULLIF(o.subtotal_gbp, 0::numeric) * o.cogs_gbp)::numeric(10,4)) AS line_cogs_gbp,
    li.line_total_gbp - COALESCE(li.line_cogs_gbp, (li.line_total_gbp / NULLIF(o.subtotal_gbp, 0::numeric) * o.cogs_gbp)::numeric(10,4), 0::numeric) AS line_gross_profit_gbp,
    o.shipping_country_code,
    o.shipping_country_name,
    o.utm_source,
    o.utm_medium,
    o.utm_campaign,
    o.utm_content
   FROM line_items li
     JOIN orders o ON o.order_id::text = li.order_id::text
     LEFT JOIN product_catalogue pc ON pc.product_id::text = li.product_id::text AND pc.variant_id::text = li.variant_id::text AND pc.brand_id::text = li.brand_id::text
  WHERE o.financial_status IS NULL OR (o.financial_status::text <> ALL (ARRAY['refunded'::character varying, 'voided'::character varying]::text[]));
