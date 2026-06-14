-- =============================================================================
-- Migration v8.3: Pro-rated line-level COGS in v_variant_sales
-- =============================================================================
-- line_items.line_cogs_gbp is always NULL — Shopify sync inserts None and no
-- other script populates it. Real COGS lives at order level (orders.cogs_gbp,
-- set by Gelato/Printify syncs). This migration teaches v_variant_sales to
-- pro-rate order-level COGS across its line items by each line's share of
-- the order subtotal:
--
--     line_cogs_gbp = (li.line_total_gbp / o.subtotal_gbp) * o.cogs_gbp
--
-- With a COALESCE on li.line_cogs_gbp first, so if line-level COGS is ever
-- populated in the future (manual override, new pipeline), it wins.
--
-- v_product_sales and v_catalogue_testing_report don't need changes — they
-- reference line_cogs_gbp by name and now get useful values automatically.
--
-- Idempotent: CREATE OR REPLACE. Re-applying is safe.
-- =============================================================================

BEGIN;

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
    -- Pro-rated COGS: prefer line-level if set, otherwise allocate order-level
    -- COGS by this line's share of the order subtotal. NULL-safe via NULLIF
    -- (orders with zero subtotal or no COGS yet pass through as NULL).
    COALESCE(
        li.line_cogs_gbp,
        ((li.line_total_gbp / NULLIF(o.subtotal_gbp, 0)) * o.cogs_gbp)::NUMERIC(10,4)
    )                                           AS line_cogs_gbp,
    (li.line_total_gbp - COALESCE(
        li.line_cogs_gbp,
        ((li.line_total_gbp / NULLIF(o.subtotal_gbp, 0)) * o.cogs_gbp)::NUMERIC(10,4),
        0
    ))                                          AS line_gross_profit_gbp,
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
    'One row per line_item with product_catalogue + order context. Excludes refunded/voided orders. line_cogs_gbp pro-rates orders.cogs_gbp by line value share (falls back to li.line_cogs_gbp when populated).';

COMMIT;

-- =============================================================================
-- Verify (run after applying)
-- =============================================================================
-- -- Spot-check that line_cogs_gbp now has values
-- SELECT COUNT(*) AS total,
--        COUNT(line_cogs_gbp) AS with_cogs,
--        ROUND(AVG(line_cogs_gbp)::numeric, 4) AS avg_line_cogs
-- FROM v_variant_sales;
--
-- -- And gross profit != revenue (it was before)
-- SELECT
--   ROUND(SUM(line_total_gbp)::numeric, 2) AS total_revenue,
--   ROUND(SUM(line_cogs_gbp)::numeric, 2)  AS total_cogs,
--   ROUND(SUM(line_gross_profit_gbp)::numeric, 2) AS total_gross_profit,
--   ROUND((SUM(line_gross_profit_gbp) / NULLIF(SUM(line_total_gbp), 0) * 100)::numeric, 2) AS margin_pct
-- FROM v_variant_sales;
