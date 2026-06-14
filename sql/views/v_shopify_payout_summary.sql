-- View: v_shopify_payout_summary
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_shopify_payout_summary.sql

CREATE OR REPLACE VIEW v_shopify_payout_summary AS
 SELECT date_trunc('month'::text, payout_date::timestamp with time zone) AS month,
    count(DISTINCT payout_id) AS payouts,
    round(sum(amount_gbp), 2) AS net_paid_to_bank,
    round(sum(charges_gross), 2) AS gross_sales,
    round(sum(charges_fees), 2) AS total_shopify_fees,
    round(sum(refunds_gross), 2) AS total_refunds,
    round(sum(adjustments_gross), 2) AS total_adjustments,
    round(sum(charges_fees) / NULLIF(sum(charges_gross), 0::numeric) * 100::numeric, 3) AS effective_fee_pct
   FROM shopify_payouts
  WHERE brand_id::text = 'your_brand_id'::text AND status::text = 'paid'::text
  GROUP BY (date_trunc('month'::text, payout_date::timestamp with time zone))
  ORDER BY (date_trunc('month'::text, payout_date::timestamp with time zone)) DESC;
