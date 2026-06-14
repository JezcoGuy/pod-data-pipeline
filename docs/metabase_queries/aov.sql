-- AOV
-- Card ID: 50
-- Collection: Root
-- Updated: 2026-05-24T10:07:08.803504Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  ROUND(shopify_aov_today_gbp :: numeric, 2) AS "AOV Today £"
FROM
  live_snapshot
WHERE
  brand_id = 'your_brand_id'
ORDER BY
  snapshot_at DESC
LIMIT
  1;
