-- View: v_monzo_monthly
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_monzo_monthly.sql

CREATE OR REPLACE VIEW v_monzo_monthly AS
 SELECT date_trunc('month'::text, created_at) AS month,
    our_category,
    count(*) AS transactions,
    round(sum(amount_gbp), 2) AS total_gbp,
    round(sum(
        CASE
            WHEN amount_gbp > 0::numeric THEN amount_gbp
            ELSE 0::numeric
        END), 2) AS total_in,
    round(sum(
        CASE
            WHEN amount_gbp < 0::numeric THEN amount_gbp
            ELSE 0::numeric
        END), 2) AS total_out
   FROM monzo_transactions
  WHERE brand_id::text = 'your_brand_id'::text AND our_category IS NOT NULL
  GROUP BY (date_trunc('month'::text, created_at)), our_category
  ORDER BY (date_trunc('month'::text, created_at)) DESC, (round(sum(amount_gbp), 2)) DESC;
