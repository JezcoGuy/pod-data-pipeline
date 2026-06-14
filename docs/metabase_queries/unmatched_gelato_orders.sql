-- Unmatched Gelato Orders
-- Card ID: 61
-- Collection: Root
-- Updated: 2026-06-06T11:15:30.210804Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  order_name AS "Order",
  revenue_gbp AS "Revenue",
  order_date :: date AS "Order Date",
  shopify_fulfillment_status AS "Shopify Status",
  fulfilment_status AS "Gelato Status",
  fulfillment_match_status AS "Match Status"
FROM
  order_fulfilment_status
WHERE
  brand_id = 'your_brand_id'
  AND fulfillment_match_status = 'unmatched'
  AND order_date < NOW() - INTERVAL '48 hours'
  AND override_flag = FALSE
UNION ALL
SELECT
  '✅ No unmatched Gelato orders',
  NULL,
  NULL,
  '',
  '',
  ''
WHERE
  NOT EXISTS (
    SELECT
      1
    FROM
      order_fulfilment_status
    WHERE
      brand_id = 'your_brand_id'
      AND fulfillment_match_status = 'unmatched'
      AND order_date < NOW() - INTERVAL '48 hours'
      AND override_flag = FALSE
  )
ORDER BY
  "Order Date" ASC NULLS LAST;
