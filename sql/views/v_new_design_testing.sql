-- View: v_new_design_testing
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_new_design_testing.sql

CREATE OR REPLACE VIEW v_new_design_testing AS
 WITH designs AS (
         SELECT DISTINCT ON (product_catalogue.product_id) product_catalogue.product_id,
            product_catalogue.product_handle,
            product_catalogue.product_title,
            product_catalogue.product_created_at::date AS created_date,
            CURRENT_DATE - product_catalogue.product_created_at::date AS days_live
           FROM product_catalogue
          WHERE product_catalogue.brand_id::text = 'your_brand_id'::text AND product_catalogue.product_created_at >= (now() - '45 days'::interval) AND product_catalogue.active = true
          ORDER BY product_catalogue.product_id
        ), catalogue_spend AS (
         SELECT ad_campaign_products.shopify_product_id AS product_id,
            sum(ad_campaign_products.spend_gbp) AS catalogue_spend,
            sum(ad_campaign_products.impressions) AS impressions,
            sum(ad_campaign_products.clicks) AS clicks,
            round(sum(ad_campaign_products.spend_gbp) / NULLIF(sum(ad_campaign_products.clicks), 0::numeric), 3) AS cpc,
            round(sum(ad_campaign_products.spend_gbp) / NULLIF(sum(ad_campaign_products.impressions), 0::numeric) * 1000::numeric, 3) AS cpm
           FROM ad_campaign_products
          WHERE ad_campaign_products.brand_id::text = 'your_brand_id'::text AND ad_campaign_products.date >= (now() - '45 days'::interval)
          GROUP BY ad_campaign_products.shopify_product_id
        ), shopify_sales AS (
         SELECT v_variant_sales.product_id,
            count(DISTINCT v_variant_sales.order_id) AS orders,
            sum(v_variant_sales.quantity) AS units,
            round(sum(v_variant_sales.line_total_gbp), 2) AS revenue,
            round(sum(v_variant_sales.line_cogs_gbp), 2) AS cogs,
            round(sum(v_variant_sales.line_gross_profit_gbp), 2) AS gross_profit,
            count(DISTINCT v_variant_sales.order_id) FILTER (WHERE v_variant_sales.utm_source::text ~~* '%facebook%'::text) AS meta_attributed_orders,
            count(DISTINCT v_variant_sales.order_id) FILTER (WHERE v_variant_sales.utm_source::text !~~* '%facebook%'::text OR v_variant_sales.utm_source IS NULL) AS organic_orders
           FROM v_variant_sales
          WHERE v_variant_sales.brand_id::text = 'your_brand_id'::text AND v_variant_sales.product_created_at >= (now() - '45 days'::interval) AND v_variant_sales.order_created_at >= (now() - '45 days'::interval)
          GROUP BY v_variant_sales.product_id
        ), ga4_data AS (
         SELECT ga4_products_daily.shopify_product_id AS product_id,
            sum(ga4_products_daily.items_viewed) AS ga4_views,
            sum(ga4_products_daily.items_added_to_cart) AS ga4_atc,
            sum(ga4_products_daily.items_purchased) AS ga4_purchases,
            count(DISTINCT ga4_products_daily.date) AS ga4_days_coverage
           FROM ga4_products_daily
          WHERE ga4_products_daily.brand_id::text = 'your_brand_id'::text AND ga4_products_daily.date >= (now() - '45 days'::interval)
          GROUP BY ga4_products_daily.shopify_product_id
        )
 SELECT d.product_handle,
    d.product_title,
    d.created_date,
    d.days_live,
    round(COALESCE(cs.catalogue_spend, 0::numeric), 2) AS catalogue_spend,
    COALESCE(cs.impressions, 0::numeric) AS impressions,
    COALESCE(cs.clicks, 0::numeric) AS clicks,
    cs.cpc,
    cs.cpm,
    COALESCE(ss.orders, 0::bigint) AS shopify_orders,
    COALESCE(ss.units, 0::bigint) AS units,
    COALESCE(ss.revenue, 0::numeric) AS revenue,
    COALESCE(ss.cogs, 0::numeric) AS cogs,
    COALESCE(ss.gross_profit, 0::numeric) AS gross_profit,
    COALESCE(ss.meta_attributed_orders, 0::bigint) AS meta_attributed_orders,
    COALESCE(ss.organic_orders, 0::bigint) AS organic_orders,
        CASE
            WHEN COALESCE(cs.catalogue_spend, 0::numeric) > 0::numeric THEN round(COALESCE(ss.revenue, 0::numeric) / cs.catalogue_spend, 2)
            ELSE NULL::numeric
        END AS shopify_roas,
    round(COALESCE(ss.gross_profit, 0::numeric) - COALESCE(cs.catalogue_spend, 0::numeric), 2) AS net_contribution,
    round(COALESCE(cs.catalogue_spend, 0::numeric) / NULLIF(d.days_live, 0)::numeric, 2) AS spend_per_day,
    round(COALESCE(ss.revenue, 0::numeric) / NULLIF(d.days_live, 0)::numeric, 2) AS revenue_per_day,
    round(COALESCE(ss.gross_profit, 0::numeric) / NULLIF(d.days_live, 0)::numeric, 2) AS profit_per_day,
    COALESCE(g.ga4_views, 0::numeric) AS ga4_views,
    COALESCE(g.ga4_atc, 0::numeric) AS ga4_atc,
    COALESCE(g.ga4_purchases, 0::numeric) AS ga4_purchases,
    round(g.ga4_atc / NULLIF(g.ga4_views, 0::numeric) * 100::numeric, 1) AS atc_rate_pct,
    COALESCE(g.ga4_days_coverage, 0::bigint) AS ga4_days_coverage,
        CASE
            WHEN COALESCE(cs.catalogue_spend, 0::numeric) = 0::numeric THEN 'no_meta_spend'::text
            WHEN COALESCE(cs.catalogue_spend, 0::numeric) < 5::numeric THEN 'insufficient_data'::text
            ELSE 'testable'::text
        END AS spend_status,
        CASE
            WHEN COALESCE(g.ga4_days_coverage, 0::bigint) < 10 THEN 'limited_ga4'::text
            ELSE 'ok'::text
        END AS ga4_status
   FROM designs d
     LEFT JOIN catalogue_spend cs ON cs.product_id::text = d.product_id::text
     LEFT JOIN shopify_sales ss ON ss.product_id::text = d.product_id::text
     LEFT JOIN ga4_data g ON g.product_id::text = d.product_id::text
  ORDER BY (round(COALESCE(ss.gross_profit, 0::numeric) - COALESCE(cs.catalogue_spend, 0::numeric), 2)) DESC NULLS LAST;
