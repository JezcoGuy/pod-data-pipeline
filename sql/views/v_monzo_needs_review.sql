-- View: v_monzo_needs_review
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_monzo_needs_review.sql

CREATE OR REPLACE VIEW v_monzo_needs_review AS
 SELECT transaction_id,
    created_at::date AS date,
    description,
    counterparty_name,
    merchant_name,
    round(amount_gbp::numeric, 2) AS amount_gbp,
    monzo_category,
    notes
   FROM monzo_transactions
  WHERE needs_review = true AND brand_id::text = 'your_brand_id'::text
  ORDER BY created_at DESC;
