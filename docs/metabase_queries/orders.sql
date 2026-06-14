-- Orders
-- Card ID: 45
-- Collection: Root
-- Updated: 2026-05-24T10:07:17.120629Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  shopify_orders_today AS "Orders Today"
FROM
  live_snapshot
WHERE
  brand_id = 'your_brand_id'
ORDER BY
  snapshot_at DESC
LIMIT
  1;
