-- Revenue
-- Card ID: 47
-- Collection: Root
-- Updated: 2026-05-24T10:06:51.724984Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  ROUND(shopify_revenue_today_gbp :: numeric, 2) AS "Revenue Today £"
FROM
  live_snapshot
WHERE
  brand_id = 'your_brand_id'
ORDER BY
  snapshot_at DESC
LIMIT
  1;
