-- View: v_paypal_summary
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_paypal_summary.sql

CREATE OR REPLACE VIEW v_paypal_summary AS
 SELECT date_trunc('month'::text, transaction_initiated) AS month,
    count(*) FILTER (WHERE transaction_type::text = 'T0006'::text) AS sales,
    count(*) FILTER (WHERE transaction_type::text = 'T1107'::text) AS refunds,
    round(sum(transaction_amount) FILTER (WHERE transaction_type::text = 'T0006'::text), 2) AS gross_sales,
    round(sum(paypal_fee) FILTER (WHERE transaction_type::text = 'T0006'::text), 2) AS total_fees,
    round(sum(net_amount) FILTER (WHERE transaction_type::text = 'T0006'::text), 2) AS net_received,
    round(avg(paypal_fee / NULLIF(transaction_amount, 0::numeric) * 100::numeric) FILTER (WHERE transaction_type::text = 'T0006'::text AND transaction_amount > 0::numeric), 3) AS avg_fee_pct
   FROM paypal_transactions
  WHERE brand_id::text = 'your_brand_id'::text AND transaction_status::text = 'S'::text
  GROUP BY (date_trunc('month'::text, transaction_initiated))
  ORDER BY (date_trunc('month'::text, transaction_initiated)) DESC;
