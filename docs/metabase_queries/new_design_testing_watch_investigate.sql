-- New Design Testing — Watch & Investigate
-- Card ID: 43
-- Collection: Root
-- Updated: 2026-05-23T20:19:32.858774Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  product_title AS "Design",
  days_live AS "Days Live",
  CONCAT('£', catalogue_spend) AS "Ad Spend",
  shopify_orders AS "Orders",
  ga4_views AS "GA4 Views",
  CONCAT(atc_rate_pct, '%') AS "ATC Rate",
  CONCAT(
    '£',
    CASE
      WHEN COALESCE(ga4_atc, 0) > 0 THEN ROUND((catalogue_spend / ga4_atc) :: numeric, 2)
      ELSE NULL
    END
  ) AS "Cost Per ATC",
  CASE
    WHEN ga4_views > 50
    AND COALESCE(atc_rate_pct, 0) < 2 THEN '🪤 Curiosity Trap — remove from Meta'
    WHEN COALESCE(atc_rate_pct, 0) > 20
    AND shopify_orders = 0
    AND days_live > 14 THEN '🔧 Iterate — strong ATC, no purchase'
    WHEN COALESCE(atc_rate_pct, 0) > 20
    AND shopify_orders = 0
    AND days_live <= 14 THEN '⏳ Too Early — high ATC, give it time'
    WHEN spend_per_day > 0.5
    AND shopify_orders = 0 THEN '💸 Burning Budget — pause now'
    ELSE '👀 Investigate'
  END AS "Flag"
FROM
  v_new_design_testing
WHERE
  (
    ga4_views > 50
    AND COALESCE(atc_rate_pct, 0) < 2
  )
  OR (
    COALESCE(atc_rate_pct, 0) > 20
    AND shopify_orders = 0
  )
  OR (
    spend_per_day > 0.5
    AND shopify_orders = 0
  )
ORDER BY
  CASE
    WHEN ga4_views > 50
    AND COALESCE(atc_rate_pct, 0) < 2 THEN 1
    WHEN COALESCE(atc_rate_pct, 0) > 20
    AND shopify_orders = 0
    AND days_live > 14 THEN 2
    WHEN spend_per_day > 0.5
    AND shopify_orders = 0 THEN 3
    ELSE 4
  END,
  days_live DESC;
