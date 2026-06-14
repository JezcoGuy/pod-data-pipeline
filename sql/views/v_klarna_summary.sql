-- View: v_klarna_summary
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_klarna_summary.sql

CREATE OR REPLACE VIEW v_klarna_summary AS
 SELECT date_trunc('month'::text, sale_date) AS month,
    count(DISTINCT capture_id) FILTER (WHERE type::text = 'SALE'::text) AS captures,
    round(sum(amount) FILTER (WHERE type::text = 'SALE'::text), 2) AS gross_sales,
    round(sum(amount) FILTER (WHERE type::text = 'FEE'::text), 2) AS total_fees,
    round(sum(amount) FILTER (WHERE type::text = 'FEE'::text) / NULLIF(sum(amount) FILTER (WHERE type::text = 'SALE'::text), 0::numeric) * 100::numeric, 3) AS effective_fee_pct
   FROM klarna_transactions
  WHERE brand_id::text = 'your_brand_id'::text AND currency_code::text = 'GBP'::text
  GROUP BY (date_trunc('month'::text, sale_date))
  ORDER BY (date_trunc('month'::text, sale_date)) DESC;
