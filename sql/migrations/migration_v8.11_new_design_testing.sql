-- =============================================================================
-- Migration v8.11: v_new_design_testing
-- =============================================================================
-- New reporting view that replaces the ad-hoc queries from the Claude Desktop
-- exploration session (23 May 2026). Combines Meta catalogue spend, Shopify
-- sales (via v_variant_sales for correct COGS + correct join), and GA4
-- engagement into one row per design (created in last 45 days).
--
-- Brief: /opt/your_brand_id/v_new_design_testing_brief.md
--
-- Three bugs the brief fixes:
--   1. Variant-level spend aggregation in v_catalogue_testing_report
--   2. line_items multiplication when joining orders+utm
--   3. Always-zero COGS (line_items.line_cogs_gbp is always NULL)
--
-- Plus ONE additional fix Claude Code spotted: the brief's shopify_sales CTE
-- still had a line-items-counted-as-orders bug in meta_attributed_orders /
-- organic_orders (SUM(CASE...) over a per-line-item view inflates the count
-- when an order spans multiple variants of the same design). Replaced with
-- COUNT(DISTINCT order_id) FILTER (WHERE ...).
-- =============================================================================

BEGIN;

CREATE OR REPLACE VIEW v_new_design_testing AS

WITH designs AS (
  -- One canonical row per design, created in last 45 days
  SELECT DISTINCT ON (product_id)
    product_id,
    product_handle,
    product_title,
    product_created_at::date AS created_date,
    (CURRENT_DATE - product_created_at::date) AS days_live
  FROM product_catalogue
  WHERE brand_id = 'your_brand_id'
    AND product_created_at >= NOW() - INTERVAL '45 days'
    AND active = true
  ORDER BY product_id
),

catalogue_spend AS (
  -- Catalogue ad spend aggregated at product level, 45-day window
  -- Aggregated BEFORE any joins to avoid multiplication
  SELECT
    shopify_product_id                                              AS product_id,
    SUM(spend_gbp)                                                  AS catalogue_spend,
    SUM(impressions)                                                AS impressions,
    SUM(clicks)                                                     AS clicks,
    ROUND(SUM(spend_gbp) / NULLIF(SUM(clicks), 0)::numeric, 3)      AS cpc,
    ROUND(SUM(spend_gbp) / NULLIF(SUM(impressions), 0) * 1000, 3)   AS cpm
  FROM ad_campaign_products
  WHERE brand_id = 'your_brand_id'
    AND date >= NOW() - INTERVAL '45 days'
  GROUP BY shopify_product_id
),

shopify_sales AS (
  -- Sales via v_variant_sales (correct pro-rated COGS, no line_items
  -- multiplication bug).
  -- IMPORTANT (Claude Code fix): meta_attributed_orders and organic_orders
  -- use COUNT(DISTINCT order_id) FILTER instead of SUM(CASE...) because
  -- v_variant_sales is per-line-item — a SUM(1)-style count would inflate
  -- the order count when one order contains multiple variants of the same
  -- design.
  SELECT
    product_id,
    COUNT(DISTINCT order_id)                                        AS orders,
    SUM(quantity)                                                   AS units,
    ROUND(SUM(line_total_gbp)::numeric, 2)                          AS revenue,
    ROUND(SUM(line_cogs_gbp)::numeric, 2)                           AS cogs,
    ROUND(SUM(line_gross_profit_gbp)::numeric, 2)                   AS gross_profit,
    COUNT(DISTINCT order_id) FILTER (
      WHERE utm_source ILIKE '%facebook%'
    )                                                                AS meta_attributed_orders,
    COUNT(DISTINCT order_id) FILTER (
      WHERE utm_source NOT ILIKE '%facebook%' OR utm_source IS NULL
    )                                                                AS organic_orders
  FROM v_variant_sales
  WHERE brand_id = 'your_brand_id'
    AND product_created_at >= NOW() - INTERVAL '45 days'
    AND order_created_at >= NOW() - INTERVAL '45 days'
  GROUP BY product_id
),

ga4_data AS (
  -- GA4 aggregated at product level with coverage day tracking.
  -- Joins via shopify_product_id (parsed from compound item_id in v8.8).
  SELECT
    shopify_product_id                                              AS product_id,
    SUM(items_viewed)                                               AS ga4_views,
    SUM(items_added_to_cart)                                        AS ga4_atc,
    SUM(items_purchased)                                            AS ga4_purchases,
    COUNT(DISTINCT date)                                            AS ga4_days_coverage
  FROM ga4_products_daily
  WHERE brand_id = 'your_brand_id'
    AND date >= NOW() - INTERVAL '45 days'
  GROUP BY shopify_product_id
)

SELECT
  d.product_handle,
  d.product_title,
  d.created_date,
  d.days_live,

  -- Meta catalogue performance
  ROUND(COALESCE(cs.catalogue_spend, 0)::numeric, 2)                AS catalogue_spend,
  COALESCE(cs.impressions, 0)                                        AS impressions,
  COALESCE(cs.clicks, 0)                                             AS clicks,
  cs.cpc,
  cs.cpm,

  -- Shopify sales (correct units, no multiplication)
  COALESCE(ss.orders, 0)                                             AS shopify_orders,
  COALESCE(ss.units, 0)                                              AS units,
  COALESCE(ss.revenue, 0)                                            AS revenue,
  COALESCE(ss.cogs, 0)                                               AS cogs,
  COALESCE(ss.gross_profit, 0)                                       AS gross_profit,
  COALESCE(ss.meta_attributed_orders, 0)                             AS meta_attributed_orders,
  COALESCE(ss.organic_orders, 0)                                     AS organic_orders,

  -- Profitability metrics
  -- shopify_roas uses TOTAL Shopify revenue (cross-channel), not just
  -- Meta-attributed — same caveat as v_catalogue_testing_report. For
  -- Meta-only ROAS, divide revenue by catalogue_spend AND filter to
  -- meta_attributed_orders only.
  CASE
    WHEN COALESCE(cs.catalogue_spend, 0) > 0
    THEN ROUND((COALESCE(ss.revenue, 0) / cs.catalogue_spend)::numeric, 2)
    ELSE NULL
  END                                                                 AS shopify_roas,

  ROUND((COALESCE(ss.gross_profit, 0)
       - COALESCE(cs.catalogue_spend, 0))::numeric, 2)               AS net_contribution,

  -- Per-day normalisation for fair comparison across different design ages
  ROUND((COALESCE(cs.catalogue_spend, 0) / NULLIF(d.days_live, 0))::numeric, 2) AS spend_per_day,
  ROUND((COALESCE(ss.revenue, 0) / NULLIF(d.days_live, 0))::numeric, 2)         AS revenue_per_day,
  ROUND((COALESCE(ss.gross_profit, 0) / NULLIF(d.days_live, 0))::numeric, 2)    AS profit_per_day,

  -- GA4 engagement
  COALESCE(g.ga4_views, 0)                                           AS ga4_views,
  COALESCE(g.ga4_atc, 0)                                             AS ga4_atc,
  COALESCE(g.ga4_purchases, 0)                                       AS ga4_purchases,
  ROUND((g.ga4_atc::numeric / NULLIF(g.ga4_views, 0) * 100), 1)      AS atc_rate_pct,
  COALESCE(g.ga4_days_coverage, 0)                                   AS ga4_days_coverage,

  -- Data quality flags
  CASE
    WHEN COALESCE(cs.catalogue_spend, 0) = 0     THEN 'no_meta_spend'
    WHEN COALESCE(cs.catalogue_spend, 0) < 5     THEN 'insufficient_data'
    ELSE 'testable'
  END                                                                 AS spend_status,

  CASE
    WHEN COALESCE(g.ga4_days_coverage, 0) < 10   THEN 'limited_ga4'
    ELSE 'ok'
  END                                                                 AS ga4_status

FROM designs d
LEFT JOIN catalogue_spend cs ON cs.product_id = d.product_id
LEFT JOIN shopify_sales   ss ON ss.product_id = d.product_id
LEFT JOIN ga4_data         g ON g.product_id  = d.product_id

ORDER BY net_contribution DESC NULLS LAST;

COMMENT ON VIEW v_new_design_testing IS
    'One row per design created in last 45 days. Combines catalogue spend (ad_campaign_products), Shopify sales (v_variant_sales for correct COGS + correct join), GA4 engagement (ga4_products_daily). spend_status / ga4_status flags help spot under-tested vs untested designs. shopify_roas is cross-channel (caveat). See migration_v8.11 + brief for full rationale.';

COMMIT;
