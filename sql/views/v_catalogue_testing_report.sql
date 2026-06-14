-- View: v_catalogue_testing_report
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_catalogue_testing_report.sql

CREATE OR REPLACE VIEW v_catalogue_testing_report AS
 WITH recent_products AS (
         SELECT product_catalogue.brand_id,
            product_catalogue.product_id,
            max(product_catalogue.product_handle::text) AS product_handle,
            max(product_catalogue.product_title::text) AS product_title,
            max(product_catalogue.product_created_at) AS product_created_at,
            bool_or(product_catalogue.active) AS any_variant_active
           FROM product_catalogue
          WHERE product_catalogue.product_created_at >= (now() - '45 days'::interval)
          GROUP BY product_catalogue.brand_id, product_catalogue.product_id
        ), meta_catalogue AS (
         SELECT ad_campaign_products.brand_id,
            ad_campaign_products.shopify_product_id AS product_id,
            sum(ad_campaign_products.spend_gbp) AS spend_gbp,
            sum(ad_campaign_products.impressions) AS impressions,
            sum(ad_campaign_products.clicks) AS clicks,
            sum(ad_campaign_products.meta_reported_purchases) AS meta_purchases,
            sum(ad_campaign_products.meta_reported_revenue) AS meta_revenue_gbp
           FROM ad_campaign_products
          WHERE ad_campaign_products.platform::text = 'meta'::text AND ad_campaign_products.shopify_product_id IS NOT NULL
          GROUP BY ad_campaign_products.brand_id, ad_campaign_products.shopify_product_id
        ), shopify_totals AS (
         SELECT v_variant_sales.brand_id,
            v_variant_sales.product_id,
            sum(v_variant_sales.quantity) AS units_sold,
            sum(v_variant_sales.line_total_gbp) AS revenue_gbp,
            sum(COALESCE(v_variant_sales.line_cogs_gbp, 0::numeric)) AS cogs_gbp
           FROM v_variant_sales
          GROUP BY v_variant_sales.brand_id, v_variant_sales.product_id
        )
 SELECT p.brand_id,
    p.product_id,
    p.product_handle,
    p.product_title,
    p.product_created_at::date AS created_date,
    EXTRACT(day FROM now() - p.product_created_at)::integer AS days_since_created,
    p.any_variant_active AS active,
    COALESCE(round(m.spend_gbp, 2), 0::numeric) AS catalogue_spend,
    COALESCE(m.impressions, 0::numeric) AS catalogue_impressions,
    COALESCE(m.clicks, 0::numeric) AS catalogue_clicks,
    round(m.spend_gbp / NULLIF(m.clicks, 0::numeric), 3) AS catalogue_cpc,
    round(m.spend_gbp / NULLIF(m.impressions, 0::numeric) * 1000::numeric, 3) AS catalogue_cpm,
    COALESCE(m.meta_purchases, 0::numeric) AS meta_reported_purchases,
    COALESCE(round(m.meta_revenue_gbp, 2), 0::numeric) AS meta_reported_revenue,
    COALESCE(s.units_sold, 0::bigint) AS shopify_units,
    COALESCE(round(s.revenue_gbp, 2), 0::numeric) AS shopify_revenue,
    COALESCE(round(s.cogs_gbp, 2), 0::numeric) AS shopify_cogs,
    round(s.revenue_gbp / NULLIF(m.spend_gbp, 0::numeric), 2) AS shopify_roas_vs_catalogue,
    round(COALESCE(s.revenue_gbp, 0::numeric) - COALESCE(s.cogs_gbp, 0::numeric) - COALESCE(m.spend_gbp, 0::numeric), 2) AS gross_contribution
   FROM recent_products p
     LEFT JOIN meta_catalogue m ON m.brand_id::text = p.brand_id::text AND m.product_id::text = p.product_id::text
     LEFT JOIN shopify_totals s ON s.brand_id::text = p.brand_id::text AND s.product_id::text = p.product_id::text
  ORDER BY (COALESCE(m.spend_gbp, 0::numeric)) DESC, (COALESCE(s.revenue_gbp, 0::numeric)) DESC;
