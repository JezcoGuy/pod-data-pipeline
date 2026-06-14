-- Est Net Profit
-- Card ID: 49
-- Collection: Root
-- Updated: 2026-05-25T09:15:34.951909Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  ROUND(
    (shopify_revenue_today_gbp * 0.579) - meta_spend_today_gbp :: numeric,
    2
  ) AS "Est. Net Today £"
FROM
  live_snapshot
WHERE
  brand_id = 'your_brand_id'
ORDER BY
  snapshot_at DESC
LIMIT
  1;
