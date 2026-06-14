-- View: v_ga4_funnel_daily
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_ga4_funnel_daily.sql

CREATE OR REPLACE VIEW v_ga4_funnel_daily AS
 SELECT date,
    brand_id,
    channel_group,
    sum(sessions) AS sessions,
    sum(engaged_sessions) AS engaged_sessions,
    sum(add_to_carts) AS add_to_carts,
    sum(checkouts) AS checkouts,
    sum(ecommerce_purchases) AS ecommerce_purchases,
    round(sum(total_revenue), 4) AS total_revenue,
    round(sum(engaged_sessions) / NULLIF(sum(sessions), 0::numeric) * 100::numeric, 2) AS engagement_rate_pct,
    round(sum(add_to_carts) / NULLIF(sum(sessions), 0::numeric) * 100::numeric, 2) AS atc_rate_pct,
    round(sum(checkouts) / NULLIF(sum(add_to_carts), 0::numeric) * 100::numeric, 2) AS atc_to_checkout_rate_pct,
    round(sum(ecommerce_purchases) / NULLIF(sum(checkouts), 0::numeric) * 100::numeric, 2) AS checkout_to_purchase_rate_pct,
    round(sum(ecommerce_purchases) / NULLIF(sum(sessions), 0::numeric) * 100::numeric, 2) AS overall_conv_rate_pct,
    round(sum(total_revenue) / NULLIF(sum(ecommerce_purchases), 0::numeric), 4) AS aov_gbp
   FROM ga4_channels_daily
  GROUP BY date, brand_id, channel_group;
