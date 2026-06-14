-- Meta Spend
-- Card ID: 48
-- Collection: Root
-- Updated: 2026-05-24T10:06:35.525946Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  ROUND(meta_spend_today_gbp :: numeric, 2) AS "Meta Spend Today £"
FROM
  live_snapshot
WHERE
  brand_id = 'your_brand_id'
ORDER BY
  snapshot_at DESC
LIMIT
  1;
