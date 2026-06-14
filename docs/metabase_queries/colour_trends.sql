-- Colour Trends
-- Card ID: 52
-- Collection: Root
-- Updated: 2026-05-25T13:22:07.797879Z
-- Extracted: 2026-06-14T10:36:25Z

WITH colour_periods AS (
  SELECT
    TRIM(
      REGEXP_REPLACE(
        REGEXP_REPLACE(
          variant_title,
          '\s*/\s*(XS|S|M|L|XL|2XL|3XL|4XL|5XL|YXS|YS|YM|YL|YXL)\s*$',
          '',
          'i'
        ),
        '^\s*(XS|S|M|L|XL|2XL|3XL|4XL|5XL|YXS|YS|YM|YL|YXL)\s*/\s*',
        '',
        'i'
      )
    ) AS colour,
    SUM(
      CASE
        WHEN order_created_at >= NOW() - INTERVAL '30 days' THEN quantity
        ELSE 0
      END
    ) AS u30,
    SUM(
      CASE
        WHEN order_created_at >= NOW() - INTERVAL '60 days' THEN quantity
        ELSE 0
      END
    ) AS u60,
    SUM(
      CASE
        WHEN order_created_at >= NOW() - INTERVAL '90 days' THEN quantity
        ELSE 0
      END
    ) AS u90,
    SUM(quantity) AS u_life
  FROM
    v_variant_sales
  WHERE
    brand_id = 'your_brand_id'
  GROUP BY
    colour
),
totals AS (
  SELECT
    SUM(u30) AS total_30,
    SUM(u60) AS total_60,
    SUM(u90) AS total_90,
    SUM(u_life) AS total_life
  FROM
    colour_periods
)
SELECT
  cp.colour AS "Colour",
  CASE
    WHEN (cp.u30 :: numeric / 30) > (cp.u90 :: numeric / 90) * 1.2 THEN '🔥 Rising'
    WHEN (cp.u30 :: numeric / 30) < (cp.u90 :: numeric / 90) * 0.8 THEN '📉 Falling'
    ELSE '➡️ Stable'
  END AS "Trend",
  cp.u30 AS "30 Days",
  ROUND((cp.u30 :: numeric / NULLIF(t.total_30, 0) * 100), 1) AS "30d %",
  cp.u60 AS "60 Days",
  ROUND((cp.u60 :: numeric / NULLIF(t.total_60, 0) * 100), 1) AS "60d %",
  cp.u90 AS "90 Days",
  ROUND((cp.u90 :: numeric / NULLIF(t.total_90, 0) * 100), 1) AS "90d %",
  cp.u_life AS "Lifetime",
  ROUND(
    (cp.u_life :: numeric / NULLIF(t.total_life, 0) * 100),
    1
  ) AS "Lifetime %"
FROM
  colour_periods cp
  CROSS JOIN totals t
WHERE
  cp.u_life >= 10
  AND cp.colour IS NOT NULL
ORDER BY
  cp.u30 DESC;
