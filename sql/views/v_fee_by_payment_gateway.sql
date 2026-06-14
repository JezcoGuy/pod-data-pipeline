-- View: v_fee_by_payment_gateway
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_fee_by_payment_gateway.sql

CREATE OR REPLACE VIEW v_fee_by_payment_gateway AS
 SELECT primary_gateway AS payment_gateway,
    count(DISTINCT order_id) AS orders,
    round(sum(revenue_gbp), 2) AS gross_revenue,
    round(sum(shopify_fee_gbp), 2) AS total_fees,
    round(avg(shopify_fee_pct), 4) AS avg_fee_pct,
    round(avg(revenue_gbp), 2) AS avg_order_value
   FROM orders o
  WHERE brand_id::text = 'your_brand_id'::text AND (financial_status::text <> ALL (ARRAY['voided'::character varying, 'refunded'::character varying]::text[])) AND shopify_fee_gbp IS NOT NULL AND primary_gateway IS NOT NULL AND primary_gateway::text <> ''::text
  GROUP BY primary_gateway
  ORDER BY (round(sum(shopify_fee_gbp), 2)) DESC;
