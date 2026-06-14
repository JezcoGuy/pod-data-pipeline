-- COGS by Region
-- Card ID: 66
-- Collection: Root
-- Updated: 2026-06-14T09:20:24.966955Z
-- Extracted: 2026-06-14T10:36:25Z

WITH regional_cogs AS (
  SELECT
    DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London') :: date AS month,
    CASE
      WHEN shipping_country_code = 'GB' THEN 'UK'
      WHEN shipping_country_code = 'US' THEN 'USA'
      WHEN shipping_country_code IN (
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
    COUNT(DISTINCT order_id) AS orders,
    ROUND(AVG(revenue_gbp) :: numeric, 2) AS avg_revenue,
    ROUND(AVG(cogs_gbp) :: numeric, 2) AS avg_cogs,
    ROUND(
      (SUM(cogs_gbp) / NULLIF(SUM(revenue_gbp), 0) * 100) :: numeric,
      1
    ) AS cogs_pct,
    ROUND(
      (
        COUNT(DISTINCT order_id) :: numeric / SUM(COUNT(DISTINCT order_id)) OVER (
          PARTITION BY DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London') :: date
        ) * 100
      ) :: numeric,
      1
    ) AS region_pct_of_orders
  FROM
    orders
  WHERE
    brand_id = 'your_brand_id'
    AND financial_status NOT IN ('voided', 'refunded')
    AND cogs_status = 'final'
  GROUP BY
    1,
    2
),
monthly_total AS (
  SELECT
    DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London') :: date AS month,
    COUNT(DISTINCT order_id) AS total_orders,
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
  CONCAT(r.region_pct_of_orders, '%') AS "% of Orders",
  CONCAT('£', r.avg_revenue) AS "Avg Revenue",
  CONCAT('£', r.avg_cogs) AS "Avg COGS",
  CONCAT(r.cogs_pct, '%') AS "COGS%",
  CONCAT(m.blended_cogs_pct, '%') AS "Blended COGS%"
FROM
  regional_cogs r
  JOIN monthly_total m ON m.month = r.month
ORDER BY
  r.month DESC,
  r.region;
