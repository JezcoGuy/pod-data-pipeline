-- Regional Heroes
-- Card ID: 53
-- Collection: Root
-- Updated: 2026-05-25T13:43:09.786868Z
-- Extracted: 2026-06-14T10:36:25Z

WITH region_sales AS (
  SELECT
    shipping_country_name AS country,
    product_id,
    TRIM(
      REGEXP_REPLACE(product_title, '\s+T-[Ss]hirt$', '', 'i')
    ) AS design,
    SUM(quantity) AS units,
    ROUND(SUM(line_total_gbp) :: numeric, 2) AS revenue
  FROM
    v_variant_sales
  WHERE
    brand_id = 'your_brand_id'
    AND order_created_at >= NOW() - INTERVAL '90 days'
    AND shipping_country_name IS NOT NULL
  GROUP BY
    shipping_country_name,
    product_id,
    product_title
),
country_totals AS (
  SELECT
    country,
    SUM(units) AS total_units,
    ROUND(SUM(revenue) :: numeric, 2) AS total_revenue,
    COUNT(DISTINCT design) AS distinct_designs
  FROM
    region_sales
  GROUP BY
    country
  HAVING
    SUM(units) >= 3
),
ranked_designs AS (
  SELECT
    rs.country,
    rs.design,
    rs.units,
    ROUND(
      (rs.units :: numeric / NULLIF(ct.total_units, 0) * 100),
      1
    ) AS pct_of_country,
    ROW_NUMBER() OVER (
      PARTITION BY rs.country
      ORDER BY
        rs.units DESC
    ) AS rank
  FROM
    region_sales rs
    JOIN country_totals ct ON ct.country = rs.country
)
SELECT
  ct.country AS "Country",
  ct.total_units AS "Units",
  CONCAT('£', ct.total_revenue) AS "Revenue",
  ct.distinct_designs AS "Designs",
  MAX(
    CASE
      WHEN rd.rank = 1 THEN rd.design || ' (' || rd.pct_of_country || '%)'
    END
  ) AS "#1",
  MAX(
    CASE
      WHEN rd.rank = 2 THEN rd.design || ' (' || rd.pct_of_country || '%)'
    END
  ) AS "#2",
  MAX(
    CASE
      WHEN rd.rank = 3 THEN rd.design || ' (' || rd.pct_of_country || '%)'
    END
  ) AS "#3",
  MAX(
    CASE
      WHEN rd.rank = 4 THEN rd.design || ' (' || rd.pct_of_country || '%)'
    END
  ) AS "#4",
  MAX(
    CASE
      WHEN rd.rank = 5 THEN rd.design || ' (' || rd.pct_of_country || '%)'
    END
  ) AS "#5"
FROM
  country_totals ct
  LEFT JOIN ranked_designs rd ON rd.country = ct.country
  AND rd.rank <= 5
GROUP BY
  ct.country,
  ct.total_units,
  ct.total_revenue,
  ct.distinct_designs
ORDER BY
  ct.total_revenue DESC;
