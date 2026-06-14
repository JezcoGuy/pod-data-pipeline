-- =============================================================================
-- Migration v8.2: Reporting views
-- =============================================================================
-- Three views to make common reporting questions one-query lookups:
--
--   1. v_variant_sales            -- one row per line_item with product + order context
--   2. v_product_sales            -- lifetime per-design sales rollup
--   3. v_catalogue_testing_report -- per-new-design (last 45d) Meta catalogue
--                                    spend vs Shopify sales
--
-- All idempotent (CREATE OR REPLACE). Re-applying is safe.
-- No schema changes — just SELECT layers on existing tables.
-- =============================================================================

BEGIN;

-- =============================================================================
-- 1. v_variant_sales — denormalised line items
-- =============================================================================
-- One row per line_item, joined to product_catalogue (for handle/type/created_at)
-- and to orders (for date, location, UTM). Metabase users aggregate freely.
--
-- Excludes refunded/voided orders so SUM(line_total_gbp) reflects actual revenue.
-- A line item's product_catalogue match falls back to NULL if the variant has
-- been archived (active=FALSE) — still surfaces the row, just without handle.

CREATE OR REPLACE VIEW v_variant_sales AS
SELECT
    li.line_item_id,
    li.order_id,
    li.brand_id,
    o.created_at                                AS order_created_at,
    o.financial_status,
    li.product_id,
    li.variant_id,
    pc.product_handle,
    li.product_title,
    li.variant_title,
    pc.product_type,
    pc.product_created_at,
    pc.active                                   AS variant_active,
    li.quantity,
    li.unit_price_gbp,
    li.line_total_gbp,
    li.line_cogs_gbp,
    (li.line_total_gbp - COALESCE(li.line_cogs_gbp, 0)) AS line_gross_profit_gbp,
    o.shipping_country_code,
    o.shipping_country_name,
    o.utm_source,
    o.utm_medium,
    o.utm_campaign,
    o.utm_content
FROM line_items li
JOIN orders o ON o.order_id = li.order_id
LEFT JOIN product_catalogue pc
    ON pc.product_id  = li.product_id
   AND pc.variant_id  = li.variant_id
   AND pc.brand_id    = li.brand_id
WHERE o.financial_status IS NULL
   OR o.financial_status NOT IN ('refunded', 'voided');

COMMENT ON VIEW v_variant_sales IS
    'One row per line_item with product_catalogue and order context. Excludes refunded/voided orders. Metabase-friendly: aggregate by date window / region / utm to taste.';


-- =============================================================================
-- 2. v_product_sales — lifetime per-design rollup
-- =============================================================================
-- One row per (product_id, brand_id) with lifetime totals across all its variants.
-- For date-windowed product reports, query v_variant_sales directly and
-- GROUP BY product_id at report time.

CREATE OR REPLACE VIEW v_product_sales AS
SELECT
    brand_id,
    product_id,
    MAX(product_handle)                                     AS product_handle,
    MAX(product_title)                                      AS product_title,
    MAX(product_type)                                       AS product_type,
    MAX(product_created_at)                                 AS product_created_at,
    BOOL_OR(variant_active)                                 AS any_variant_active,
    COUNT(DISTINCT order_id)                                AS orders,
    COUNT(DISTINCT variant_id)                              AS variants_sold,
    SUM(quantity)                                           AS units_sold,
    ROUND(SUM(line_total_gbp)::numeric, 4)                  AS revenue_gbp,
    ROUND(SUM(COALESCE(line_cogs_gbp, 0))::numeric, 4)      AS cogs_gbp,
    ROUND(SUM(line_gross_profit_gbp)::numeric, 4)           AS gross_profit_gbp,
    MIN(order_created_at)                                   AS first_sold_at,
    MAX(order_created_at)                                   AS last_sold_at
FROM v_variant_sales
GROUP BY brand_id, product_id;

COMMENT ON VIEW v_product_sales IS
    'Lifetime sales totals rolled up to product (design) grain. For date-windowed reports, query v_variant_sales directly and aggregate at report time.';


-- =============================================================================
-- 3. v_catalogue_testing_report — new-design performance dashboard
-- =============================================================================
-- For each design created in the last 45 days: Meta catalogue spend, impressions,
-- clicks, plus Shopify sales context. The 45-day filter is the key design choice
-- — it isolates the *testing* workflow from established hero products being
-- scaled via statics (which would otherwise skew ROAS rankings because their
-- Shopify revenue comes mostly from non-catalogue sources).
--
-- IMPORTANT — shopify_revenue counts all sales of the product regardless of
-- channel (Meta, organic, email, direct). The shopify_roas_vs_catalogue figure
-- is therefore the *upper bound* of catalogue ROAS. For Meta-only attribution,
-- additionally filter v_variant_sales rows by utm_source ILIKE '%facebook%'.
-- GA4 will resolve this properly when ga4_sync.py is online.

CREATE OR REPLACE VIEW v_catalogue_testing_report AS
WITH recent_products AS (
    SELECT
        brand_id,
        product_id,
        MAX(product_handle)     AS product_handle,
        MAX(product_title)      AS product_title,
        MAX(product_created_at) AS product_created_at,
        BOOL_OR(active)         AS any_variant_active
    FROM product_catalogue
    WHERE product_created_at >= NOW() - INTERVAL '45 days'
    GROUP BY brand_id, product_id
),
meta_catalogue AS (
    SELECT
        brand_id,
        shopify_product_id                  AS product_id,
        SUM(spend_gbp)                      AS spend_gbp,
        SUM(impressions)                    AS impressions,
        SUM(clicks)                         AS clicks,
        SUM(meta_reported_purchases)        AS meta_purchases,
        SUM(meta_reported_revenue)          AS meta_revenue_gbp
    FROM ad_campaign_products
    WHERE platform = 'meta'
      AND shopify_product_id IS NOT NULL
    GROUP BY brand_id, shopify_product_id
),
shopify_totals AS (
    SELECT
        brand_id,
        product_id,
        SUM(quantity)                           AS units_sold,
        SUM(line_total_gbp)                     AS revenue_gbp,
        SUM(COALESCE(line_cogs_gbp, 0))         AS cogs_gbp
    FROM v_variant_sales
    GROUP BY brand_id, product_id
)
SELECT
    p.brand_id,
    p.product_id,
    p.product_handle,
    p.product_title,
    p.product_created_at::date                                       AS created_date,
    EXTRACT(DAY FROM (NOW() - p.product_created_at))::int            AS days_since_created,
    p.any_variant_active                                             AS active,

    -- Meta catalogue activity
    COALESCE(ROUND(m.spend_gbp::numeric, 2), 0)                      AS catalogue_spend,
    COALESCE(m.impressions, 0)                                       AS catalogue_impressions,
    COALESCE(m.clicks, 0)                                            AS catalogue_clicks,
    ROUND((m.spend_gbp / NULLIF(m.clicks, 0))::numeric, 3)           AS catalogue_cpc,
    ROUND((m.spend_gbp / NULLIF(m.impressions, 0) * 1000)::numeric, 3) AS catalogue_cpm,
    COALESCE(m.meta_purchases, 0)                                    AS meta_reported_purchases,
    COALESCE(ROUND(m.meta_revenue_gbp::numeric, 2), 0)               AS meta_reported_revenue,

    -- Shopify sales (any source — see view comment on attribution caveat)
    COALESCE(s.units_sold, 0)                                        AS shopify_units,
    COALESCE(ROUND(s.revenue_gbp::numeric, 2), 0)                    AS shopify_revenue,
    COALESCE(ROUND(s.cogs_gbp::numeric, 2), 0)                       AS shopify_cogs,

    -- Derived (caveat: shopify_revenue is cross-channel, see view comment)
    ROUND((s.revenue_gbp / NULLIF(m.spend_gbp, 0))::numeric, 2)      AS shopify_roas_vs_catalogue,
    ROUND((COALESCE(s.revenue_gbp, 0)
           - COALESCE(s.cogs_gbp, 0)
           - COALESCE(m.spend_gbp, 0))::numeric, 2)                  AS gross_contribution
FROM recent_products p
LEFT JOIN meta_catalogue m  ON m.brand_id = p.brand_id AND m.product_id = p.product_id
LEFT JOIN shopify_totals s  ON s.brand_id = p.brand_id AND s.product_id = p.product_id
ORDER BY COALESCE(m.spend_gbp, 0) DESC, COALESCE(s.revenue_gbp, 0) DESC;

COMMENT ON VIEW v_catalogue_testing_report IS
    'New-design (last 45d) performance dashboard: catalogue spend + Shopify sales per product. shopify_revenue is cross-channel, not Meta-attributed — see view source for the attribution caveat. Replace with GA4-attributed revenue once ga4_sync.py is online.';

COMMIT;

-- =============================================================================
-- Verify (run after applying)
-- =============================================================================
-- \dv v_*
-- SELECT COUNT(*) FROM v_variant_sales;
-- SELECT COUNT(*) FROM v_product_sales;
-- SELECT * FROM v_catalogue_testing_report LIMIT 5;
