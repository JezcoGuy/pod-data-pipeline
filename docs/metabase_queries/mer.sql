-- MER
-- Card ID: 44
-- Collection: Root
-- Updated: 2026-05-24T10:06:59.695191Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  mer AS "MER"
FROM
  live_snapshot
WHERE
  brand_id = 'your_brand_id'
ORDER BY
  snapshot_at DESC
LIMIT
  1;
