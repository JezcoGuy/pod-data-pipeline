-- View: v_sessions_daily
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_sessions_daily.sql

CREATE OR REPLACE VIEW v_sessions_daily AS
 SELECT ga4_sessions_backfill.date,
    ga4_sessions_backfill.brand_id,
    ga4_sessions_backfill.data_source,
    ga4_sessions_backfill.sessions,
    ga4_sessions_backfill.atc,
    ga4_sessions_backfill.atc_rate_pct,
    ga4_sessions_backfill.reached_checkout,
    ga4_sessions_backfill.reached_checkout_pct,
    ga4_sessions_backfill.purchases,
    ga4_sessions_backfill.cr_pct,
    ga4_sessions_backfill.returning_orders,
    ga4_sessions_backfill.returning_pct,
    ga4_sessions_backfill.designs_uploaded,
    ga4_sessions_backfill.ads_launched,
    ga4_sessions_backfill.emails_sent,
    NULL::integer AS new_users
   FROM ga4_sessions_backfill
UNION ALL
 SELECT (ga4_channels_daily.date AT TIME ZONE 'Europe/London'::text)::date AS date,
    ga4_channels_daily.brand_id,
    'ga4'::character varying AS data_source,
    sum(ga4_channels_daily.sessions)::integer AS sessions,
    sum(ga4_channels_daily.add_to_carts)::integer AS atc,
    round(sum(ga4_channels_daily.add_to_carts) / NULLIF(sum(ga4_channels_daily.sessions), 0::numeric) * 100::numeric, 2) AS atc_rate_pct,
    sum(ga4_channels_daily.checkouts)::integer AS reached_checkout,
    round(sum(ga4_channels_daily.checkouts) / NULLIF(sum(ga4_channels_daily.sessions), 0::numeric) * 100::numeric, 2) AS reached_checkout_pct,
    sum(ga4_channels_daily.ecommerce_purchases)::integer AS purchases,
    round(sum(ga4_channels_daily.ecommerce_purchases) / NULLIF(sum(ga4_channels_daily.sessions), 0::numeric) * 100::numeric, 2) AS cr_pct,
    NULL::integer AS returning_orders,
    NULL::numeric AS returning_pct,
    NULL::integer AS designs_uploaded,
    NULL::integer AS ads_launched,
    NULL::integer AS emails_sent,
    sum(ga4_channels_daily.new_users)::integer AS new_users
   FROM ga4_channels_daily
  WHERE ga4_channels_daily.brand_id::text = 'your_brand_id'::text AND (ga4_channels_daily.date AT TIME ZONE 'Europe/London'::text)::date > COALESCE(( SELECT max(ga4_sessions_backfill.date) AS max
           FROM ga4_sessions_backfill
          WHERE ga4_sessions_backfill.brand_id::text = 'your_brand_id'::text), '1900-01-01'::date)
  GROUP BY ((ga4_channels_daily.date AT TIME ZONE 'Europe/London'::text)::date), ga4_channels_daily.brand_id
  ORDER BY 1 DESC;
