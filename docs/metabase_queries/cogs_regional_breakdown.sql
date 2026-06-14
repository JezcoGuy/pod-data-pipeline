-- COGS Regional Breakdown
-- Card ID: 67
-- Collection: Root
-- Updated: 2026-06-14T09:29:30.844816Z
-- Extracted: 2026-06-14T10:36:25Z

WITH regional_breakdown AS (
  SELECT
    CASE
      WHEN o.shipping_country_code = 'GB' THEN 'UK'
      WHEN o.shipping_country_code = 'US' THEN 'USA'
      WHEN o.shipping_country_code IN (
        'AT',
        'BE',
        'BG',
        'HR',
        'CY',
        'CZ',
        'DK',
        'EE',
        'FI',
        'FR',
        'DE',
        'GR',
        'HU',
        'IE',
        'IT',
        'LV',
        'LT',
        'LU',
        'MT',
        'NL',
        'PL',
        'PT',
        'RO',
        'SK',
        'SI',
        'ES',
        'SE'
      ) THEN 'EU'
      ELSE 'ROW'
    END AS region,
    DATE_TRUNC('month', o.created_at AT TIME ZONE 'Europe/London') :: date AS month,
    COUNT(DISTINCT o.order_id) AS orders,
    ROUND(AVG(o.revenue_gbp) :: numeric, 2) AS avg_revenue,
    ROUND(AVG(o.cogs_gbp) :: numeric, 2) AS avg_total_cogs,
    ROUND(
      (SUM(o.cogs_gbp) / NULLIF(SUM(o.revenue_gbp), 0) * 100) :: numeric,
      1
    ) AS cogs_pct,
    -- Detailed breakdown from fulfilments where available
    ROUND(AVG(f.products_price) :: numeric, 2) AS avg_product_cost,
    ROUND(AVG(f.shipping_price) :: numeric, 2) AS avg_shipping_cost,
    ROUND(AVG(f.vat_amount) :: numeric, 2) AS avg_vat,
    -- As % of revenue
    ROUND(
      (
        AVG(f.products_price) / NULLIF(AVG(o.revenue_gbp), 0) * 100
      ) :: numeric,
      1
    ) AS product_cost_pct,
    ROUND(
      (
        AVG(f.shipping_price) / NULLIF(AVG(o.revenue_gbp), 0) * 100
      ) :: numeric,
      1
    ) AS shipping_pct,
    ROUND(
      (
        AVG(f.vat_amount) / NULLIF(AVG(o.revenue_gbp), 0) * 100
      ) :: numeric,
      1
    ) AS vat_pct,
    COUNT(
      DISTINCT CASE
        WHEN f.fulfilment_id IS NOT NULL THEN o.order_id
      END
    ) AS orders_with_breakdown,
    -- Order mix context
    ROUND(
      (
        COUNT(DISTINCT o.order_id) :: numeric / SUM(COUNT(DISTINCT o.order_id)) OVER (
          PARTITION BY DATE_TRUNC('month', o.created_at AT TIME ZONE 'Europe/London') :: date
        ) * 100
      ) :: numeric,
      1
    ) AS pct_of_monthly_orders
  FROM
    orders o
    LEFT JOIN fulfilments f ON f.shopify_order_id = o.order_id
    AND f.brand_id = o.brand_id
    AND f.products_price > 0
    AND f.is_cancelled = FALSE
  WHERE
    o.brand_id = 'your_brand_id'
    AND o.financial_status NOT IN ('voided', 'refunded')
    AND o.cogs_status = 'final'
  GROUP BY
    1,
    2
),
monthly_blended AS (
  SELECT
    DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London') :: date AS month,
    ROUND(
      (SUM(cogs_gbp) / NULLIF(SUM(revenue_gbp), 0) * 100) :: numeric,
      1
    ) AS blended_cogs_pct
  FROM
    orders
  WHERE
    brand_id = 'your_brand_id'
    AND financial_status NOT IN ('voided', 'refunded')
    AND cogs_status = 'final'
  GROUP BY
    1
)
SELECT
  TO_CHAR(r.month, 'Mon YYYY') AS "Month",
  r.region AS "Region",
  r.orders AS "Orders",
  CONCAT(r.pct_of_monthly_orders, '%') AS "% of Orders",
  CONCAT('£', r.avg_revenue) AS "Avg Revenue",
  -- Total COGS
  CONCAT('£', r.avg_total_cogs) AS "Avg COGS",
  CONCAT(r.cogs_pct, '%') AS "COGS%",
  -- Breakdown columns (from fulfilments where available)
  CASE
    WHEN r.avg_product_cost IS NOT NULL THEN CONCAT('£', r.avg_product_cost)
    ELSE '-'
  END AS "Avg Product Cost",
  CASE
    WHEN r.product_cost_pct IS NOT NULL THEN CONCAT(r.product_cost_pct, '%')
    ELSE '-'
  END AS "Product Cost%",
  CASE
    WHEN r.avg_shipping_cost IS NOT NULL THEN CONCAT('£', r.avg_shipping_cost)
    ELSE '-'
  END AS "Avg Shipping",
  CASE
    WHEN r.shipping_pct IS NOT NULL THEN CONCAT(r.shipping_pct, '%')
    ELSE '-'
  END AS "Shipping%",
  CASE
    WHEN r.avg_vat IS NOT NULL THEN CONCAT('£', r.avg_vat)
    ELSE '-'
  END AS "Avg VAT",
  CASE
    WHEN r.vat_pct IS NOT NULL THEN CONCAT(r.vat_pct, '%')
    ELSE '-'
  END AS "VAT%",
  -- Context
  CONCAT(m.blended_cogs_pct, '%') AS "Blended COGS%",
  r.orders_with_breakdown AS "Orders w/ Breakdown"
FROM
  regional_breakdown r
  JOIN monthly_blended m ON m.month = r.month
ORDER BY
  r.month DESC,
  r.region;
