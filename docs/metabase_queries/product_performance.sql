-- Product Performance
-- Card ID: 51
-- Collection: Root
-- Updated: 2026-05-25T13:02:17.16517Z
-- Extracted: 2026-06-14T10:36:25Z

WITH product_base AS (
  SELECT
    DISTINCT ON (product_id) product_id,
    TRIM(
      REGEXP_REPLACE(product_title, '\s+T-[Ss]hirt$', '', 'i')
    ) AS design,
    (CURRENT_DATE - product_created_at :: date) AS days_live
  FROM
    product_catalogue
  WHERE
    brand_id = 'your_brand_id'
    AND active = true
  ORDER BY
    product_id
),
period_sales AS (
  SELECT
    product_id,
    SUM(
      CASE
        WHEN order_created_at >= NOW() - INTERVAL '7 days' THEN quantity
        ELSE 0
      END
    ) AS u7,
    SUM(
      CASE
        WHEN order_created_at >= NOW() - INTERVAL '30 days' THEN quantity
        ELSE 0
      END
    ) AS u30,
    SUM(
      CASE
        WHEN order_created_at >= NOW() - INTERVAL '60 days'
        AND order_created_at < NOW() - INTERVAL '30 days' THEN quantity
        ELSE 0
      END
    ) AS u30_prior,
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
    product_id
),
current_rank AS (
  SELECT
    product_id,
    RANK() OVER (
      ORDER BY
        SUM(
          CASE
            WHEN order_created_at >= NOW() - INTERVAL '30 days' THEN quantity
            ELSE 0
          END
        ) DESC
    ) AS rank_now
  FROM
    v_variant_sales
  WHERE
    brand_id = 'your_brand_id'
  GROUP BY
    product_id
),
prior_rank AS (
  SELECT
    product_id,
    RANK() OVER (
      ORDER BY
        SUM(
          CASE
            WHEN order_created_at >= NOW() - INTERVAL '60 days'
            AND order_created_at < NOW() - INTERVAL '30 days' THEN quantity
            ELSE 0
          END
        ) DESC
    ) AS rank_prior
  FROM
    v_variant_sales
  WHERE
    brand_id = 'your_brand_id'
  GROUP BY
    product_id
),
colour_sales AS (
  SELECT
    product_id,
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
    SUM(quantity) AS units_sold
  FROM
    v_variant_sales
  WHERE
    brand_id = 'your_brand_id'
    AND order_created_at >= NOW() - INTERVAL '90 days'
  GROUP BY
    product_id,
    colour
),
product_colour_totals AS (
  SELECT
    product_id,
    SUM(units_sold) AS total_units
  FROM
    colour_sales
  GROUP BY
    product_id
),
best_colour AS (
  SELECT
    DISTINCT ON (cs.product_id) cs.product_id,
    cs.colour || ' (' || ROUND(
      (
        cs.units_sold :: numeric / NULLIF(pct.total_units, 0) * 100
      ),
      0
    ) || '%)' AS best_colour
  FROM
    colour_sales cs
    JOIN product_colour_totals pct ON pct.product_id = cs.product_id
  ORDER BY
    cs.product_id,
    cs.units_sold DESC
),
combined AS (
  SELECT
    pb.design,
    pb.days_live,
    COALESCE(ps.u7, 0) AS u7,
    COALESCE(ps.u30, 0) AS u30,
    COALESCE(ps.u30_prior, 0) AS u30_prior,
    COALESCE(ps.u90, 0) AS u90,
    COALESCE(ps.u_life, 0) AS u_life,
    ROUND((COALESCE(ps.u7, 0) :: numeric / 7.0), 3) AS recent_vel,
    ROUND(
      (
        COALESCE(ps.u90, 0) :: numeric / NULLIF(LEAST(pb.days_live, 90), 0)
      ),
      3
    ) AS overall_vel,
    cr.rank_now,
    pr.rank_prior,
    COALESCE(bc.best_colour, '—') AS best_colour
  FROM
    product_base pb
    LEFT JOIN period_sales ps ON ps.product_id = pb.product_id
    LEFT JOIN current_rank cr ON cr.product_id = pb.product_id
    LEFT JOIN prior_rank pr ON pr.product_id = pb.product_id
    LEFT JOIN best_colour bc ON bc.product_id = pb.product_id
  WHERE
    COALESCE(ps.u_life, 0) >= 3
    AND COALESCE(ps.u30, 0) > 0
)
SELECT
  design AS "Design",
  CASE
    WHEN u30 >= 5
    AND recent_vel > overall_vel * 1.5 THEN '🔥 Accelerating'
    WHEN u30 >= 5
    AND recent_vel >= overall_vel * 0.8 THEN '➡️ Steady'
    WHEN u30 >= 5
    AND recent_vel < overall_vel * 0.8 THEN '📉 Declining'
    WHEN u30_prior = 0
    AND u30 >= 3 THEN '🆕 New Entry'
    ELSE '⏳ Early'
  END AS "Momentum",
  CASE
    WHEN u30_prior = 0
    AND u30 > 0 THEN '🆕'
    WHEN rank_now < rank_prior THEN '↑ ' || (rank_prior - rank_now) :: text
    WHEN rank_now > rank_prior THEN '↓ ' || (rank_now - rank_prior) :: text
    ELSE '→'
  END AS "vs Last Month",
  u7 AS "7 Days",
  u30 AS "30 Days",
  u30_prior AS "Last Month",
  u_life AS "Lifetime",
  best_colour AS "Best Colour (90d)",
  days_live AS "Days Live"
FROM
  combined
ORDER BY
  u30 DESC;
