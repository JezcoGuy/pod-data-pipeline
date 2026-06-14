-- New Design Testing — Testable (£5+ Spend)
-- Card ID: 40
-- Collection: Root
-- Updated: 2026-05-31T06:13:13.51533Z
-- Extracted: 2026-06-14T10:36:25Z

WITH top_colours AS (
  WITH variant_spend AS (
    SELECT
      pc.product_handle,
      TRIM(
        REGEXP_REPLACE(
          REGEXP_REPLACE(
            pc.variant_title,
            '\s*/\s*(XS|S|M|L|XL|2XL|3XL|4XL|5XL|YXS|YS|YM|YL|YXL)\s*$',
            '',
            'i'
          ),
          '^\s*(XS|S|M|L|XL|2XL|3XL|4XL|5XL|YXS|YS|YM|YL|YXL)\s*/\s*',
          '',
          'i'
        )
      ) AS colour,
      SUM(acp.impressions) AS impressions
    FROM
      ad_campaign_products acp
      JOIN product_catalogue pc ON pc.variant_id = acp.shopify_variant_id
      AND pc.brand_id = acp.brand_id
    WHERE
      acp.brand_id = 'your_brand_id'
      AND acp.date >= NOW() - INTERVAL '45 days'
      AND pc.product_created_at >= NOW() - INTERVAL '45 days'
    GROUP BY
      pc.product_handle,
      colour
  ),
  colour_totals AS (
    SELECT
      product_handle,
      SUM(impressions) AS total_impressions
    FROM
      variant_spend
    GROUP BY
      product_handle
  ),
  ranked AS (
    SELECT
      vs.product_handle,
      vs.colour,
      ROUND(
        (
          vs.impressions :: numeric / NULLIF(ct.total_impressions, 0) * 100
        ),
        0
      ) AS share_pct,
      ROW_NUMBER() OVER (
        PARTITION BY vs.product_handle
        ORDER BY
          vs.impressions DESC
      ) AS rank
    FROM
      variant_spend vs
      JOIN colour_totals ct ON ct.product_handle = vs.product_handle
  )
  SELECT
    product_handle,
    MAX(
      CASE
        WHEN rank = 1 THEN colour || ' (' || share_pct || '%)'
      END
    ) AS top_colour,
    MAX(
      CASE
        WHEN rank = 2
        AND share_pct >= 15 THEN colour || ' (' || share_pct || '%)'
        ELSE NULL
      END
    ) AS second_colour
  FROM
    ranked
  WHERE
    rank <= 2
  GROUP BY
    product_handle
)
SELECT
  v.product_title AS "Design",
  v.days_live AS "Days Live",
  CONCAT('£', v.catalogue_spend) AS "Ad Spend",
  v.shopify_orders AS "Orders",
  CASE
    WHEN v.shopify_roas IS NOT NULL
    AND v.shopify_orders > 0 THEN CONCAT(v.shopify_roas, 'x')
    ELSE '—'
  END AS "ROAS",
  CASE
    WHEN v.shopify_orders > 0 THEN CONCAT(
      '£',
      ROUND((v.catalogue_spend / v.shopify_orders) :: numeric, 2)
    )
    ELSE '—'
  END AS "Cost Per Order",
  CONCAT(COALESCE(v.atc_rate_pct :: text, '0'), '%') AS "ATC Rate",
  COALESCE(tc.top_colour, '—') AS "Top Colour",
  COALESCE(tc.second_colour, '—') AS "2nd Colour",
  CASE
    WHEN v.net_contribution > 0 THEN '🟢 Test'
    WHEN v.net_contribution > -15
    AND v.shopify_orders > 0
    AND COALESCE(v.atc_rate_pct, 0) > 2 THEN '🟡 Watch'
    WHEN v.net_contribution > -15
    AND COALESCE(v.atc_rate_pct, 0) > 15 THEN '🟡 Watch ATC'
    ELSE '🔴 Cut'
  END AS "Action",
  v.ga4_status AS "GA4 Coverage"
FROM
  v_new_design_testing v
  LEFT JOIN top_colours tc ON tc.product_handle = v.product_handle
WHERE
  v.spend_status = 'testable'
ORDER BY
  v.net_contribution DESC;
