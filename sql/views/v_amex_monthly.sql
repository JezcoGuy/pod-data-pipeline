-- View: v_amex_monthly
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_amex_monthly.sql

CREATE OR REPLACE VIEW v_amex_monthly AS
 SELECT date_trunc('month'::text, transaction_date::timestamp with time zone) AS month,
    card,
    our_category,
    count(*) AS transactions,
    round(sum(
        CASE
            WHEN amount_gbp > 0::numeric THEN amount_gbp
            ELSE 0::numeric
        END), 2) AS total_charges,
    round(sum(
        CASE
            WHEN amount_gbp < 0::numeric THEN amount_gbp
            ELSE 0::numeric
        END), 2) AS total_credits
   FROM amex_transactions
  WHERE brand_id::text = 'your_brand_id'::text AND our_category IS NOT NULL AND our_category::text <> 'AMEX_PAYMENT_RECEIVED'::text
  GROUP BY (date_trunc('month'::text, transaction_date::timestamp with time zone)), card, our_category
  ORDER BY (date_trunc('month'::text, transaction_date::timestamp with time zone)) DESC, (round(sum(
        CASE
            WHEN amount_gbp > 0::numeric THEN amount_gbp
            ELSE 0::numeric
        END), 2)) DESC;
