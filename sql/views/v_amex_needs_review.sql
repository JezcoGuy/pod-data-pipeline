-- View: v_amex_needs_review
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_amex_needs_review.sql

CREATE OR REPLACE VIEW v_amex_needs_review AS
 SELECT transaction_date AS date,
    card,
    merchant_name,
    description,
    amount_gbp,
    source_file
   FROM amex_transactions
  WHERE needs_review = true AND brand_id::text = 'your_brand_id'::text
  ORDER BY transaction_date DESC;
