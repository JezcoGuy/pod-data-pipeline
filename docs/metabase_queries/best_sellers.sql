-- Best Sellers
-- Card ID: 63
-- Collection: Root
-- Updated: 2026-06-06T12:35:28.342704Z
-- Extracted: 2026-06-14T10:36:25Z

WITH best_seller_sales AS (
  SELECT
    vs.product_handle,
    MAX(vs.product_title) AS product_title,
    -- Age
    (CURRENT_DATE - MIN(pc2.product_created_at) :: date) AS days_live,
    -- Sales windows
    SUM(
      CASE
        WHEN vs.order_created_at >= NOW() - INTERVAL '7 days' THEN vs.quantity
        ELSE 0
      END
    ) AS units_7d,
    SUM(
      CASE
        WHEN vs.order_created_at >= NOW() - INTERVAL '30 days' THEN vs.quantity
        ELSE 0
      END
    ) AS units_30d,
    SUM(vs.quantity) AS units_lifetime,
    -- Regional split (30d)
    ROUND(
      SUM(
        CASE
          WHEN vs.order_created_at >= NOW() - INTERVAL '30 days'
          AND vs.shipping_country_code = 'GB' THEN vs.quantity
          ELSE 0
        END
      ) :: numeric / NULLIF(
        SUM(
          CASE
            WHEN vs.order_created_at >= NOW() - INTERVAL '30 days' THEN vs.quantity
            ELSE 0
          END
        ),
        0
      ) * 100,
      0
    ) AS uk_pct,
    ROUND(
      SUM(
        CASE
          WHEN vs.order_created_at >= NOW() - INTERVAL '30 days'
          AND vs.shipping_country_code = 'US' THEN vs.quantity
          ELSE 0
        END
      ) :: numeric / NULLIF(
        SUM(
          CASE
            WHEN vs.order_created_at >= NOW() - INTERVAL '30 days' THEN vs.quantity
            ELSE 0
          END
        ),
        0
      ) * 100,
      0
    ) AS usa_pct,
    ROUND(
      SUM(
        CASE
          WHEN vs.order_created_at >= NOW() - INTERVAL '30 days'
          AND vs.shipping_country_code IN (
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
          ) THEN vs.quantity
          ELSE 0
        END
      ) :: numeric / NULLIF(
        SUM(
          CASE
            WHEN vs.order_created_at >= NOW() - INTERVAL '30 days' THEN vs.quantity
            ELSE 0
          END
        ),
        0
      ) * 100,
      0
    ) AS eu_pct,
    ROUND(
      SUM(
        CASE
          WHEN vs.order_created_at >= NOW() - INTERVAL '30 days'
          AND vs.shipping_country_code NOT IN (
            'GB',
            'US',
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
          ) THEN vs.quantity
          ELSE 0
        END
      ) :: numeric / NULLIF(
        SUM(
          CASE
            WHEN vs.order_created_at >= NOW() - INTERVAL '30 days' THEN vs.quantity
            ELSE 0
          END
        ),
        0
      ) * 100,
      0
    ) AS row_pct
  FROM
    v_variant_sales vs
    JOIN (
      SELECT
        DISTINCT ON (product_handle) product_handle,
        product_created_at,
        product_tags
      FROM
        product_catalogue
      WHERE
        brand_id = 'your_brand_id'
      ORDER BY
        product_handle,
        product_created_at ASC
    ) pc2 ON pc2.product_handle = vs.product_handle
  WHERE
    vs.brand_id = 'your_brand_id'
    AND vs.financial_status NOT IN ('voided', 'refunded')
    AND pc2.product_tags ILIKE '%best_seller%'
  GROUP BY
    vs.product_handle
),
with_momentum AS (
  SELECT
    *,
    ROUND(units_30d :: numeric / 30, 2) AS avg_30d_per_day,
    ROUND(units_7d :: numeric / 7, 2) AS avg_7d_per_day
  FROM
    best_seller_sales
)
SELECT
  ROW_NUMBER() OVER (
    ORDER BY
      units_7d DESC
  ) AS "Rank",
  product_title AS "Product",
  days_live AS "Days Live",
  units_7d AS "7d Units",
  units_30d AS "30d Units",
  units_lifetime AS "Lifetime Units",
  CASE
    WHEN avg_7d_per_day > avg_30d_per_day * 1.2 THEN '↑'
    WHEN avg_7d_per_day < avg_30d_per_day * 0.8 THEN '↓'
    ELSE '→'
  END AS "Momentum",
  CONCAT(uk_pct, '%') AS "UK%",
  CONCAT(usa_pct, '%') AS "USA%",
  CONCAT(eu_pct, '%') AS "EU%",
  CONCAT(row_pct, '%') AS "ROW%"
FROM
  with_momentum
ORDER BY
  units_7d DESC;
