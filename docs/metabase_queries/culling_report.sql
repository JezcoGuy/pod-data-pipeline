-- Culling Report
-- Card ID: 62
-- Collection: Root
-- Updated: 2026-06-06T12:20:41.985372Z
-- Extracted: 2026-06-14T10:36:25Z

WITH distinct_products AS (
  SELECT
    DISTINCT ON (product_handle) product_handle,
    product_title,
    product_tags,
    product_created_at :: date AS created_date
  FROM
    product_catalogue
  WHERE
    brand_id = 'your_brand_id'
  ORDER BY
    product_handle,
    product_created_at ASC
),
product_sales AS (
  SELECT
    dp.product_handle,
    dp.product_title,
    dp.product_tags,
    dp.created_date,
    (CURRENT_DATE - dp.created_date) AS age_days,
    COALESCE(SUM(vs.quantity), 0) AS lifetime_units,
    ROUND(COALESCE(SUM(vs.line_total_gbp), 0) :: numeric, 2) AS lifetime_revenue,
    MAX(vs.order_created_at :: date) AS last_sale_date
  FROM
    distinct_products dp
    LEFT JOIN v_variant_sales vs ON vs.product_handle = dp.product_handle
    AND vs.brand_id = 'your_brand_id'
    AND vs.financial_status NOT IN ('voided', 'refunded')
  GROUP BY
    dp.product_handle,
    dp.product_title,
    dp.product_tags,
    dp.created_date
)
SELECT
  product_title AS "Product",
  age_days AS "Days Live",
  lifetime_units AS "Units Sold",
  CONCAT('£', lifetime_revenue) AS "Revenue",
  last_sale_date AS "Last Sale",
  CASE
    WHEN lifetime_units = 0 THEN '🔴 Remove — zero sales'
    ELSE '🔴 Remove — ' || lifetime_units || ' sale(s) only'
  END AS "Action"
FROM
  product_sales
WHERE
  age_days > 200
  AND lifetime_units <= 2
  AND (
    product_tags IS NULL
    OR product_tags NOT ILIKE '%your_my%'
  )
ORDER BY
  lifetime_units ASC,
  age_days DESC;
