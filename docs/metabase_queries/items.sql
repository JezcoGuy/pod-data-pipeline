-- Items
-- Card ID: 46
-- Collection: Root
-- Updated: 2026-05-24T10:07:24.842643Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  shopify_items_today AS "Items Today"
FROM
  live_snapshot
WHERE
  brand_id = 'your_brand_id'
ORDER BY
  snapshot_at DESC
LIMIT
  1;
