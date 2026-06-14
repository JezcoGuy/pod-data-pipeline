-- New Design Testing — Organic Performers (No Meta Spend)
-- Card ID: 42
-- Collection: Root
-- Updated: 2026-05-23T20:17:36.867589Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  product_title AS "Design",
  days_live AS "Days Live",
  shopify_orders AS "Orders",
  CONCAT('£', revenue) AS "Revenue",
  CONCAT('£', gross_profit) AS "Gross Profit",
  CONCAT(atc_rate_pct, '%') AS "ATC Rate",
  '🎯 Test on Meta' AS "Action"
FROM
  v_new_design_testing
WHERE
  spend_status = 'no_meta_spend'
  AND shopify_orders > 0
ORDER BY
  gross_profit DESC;
