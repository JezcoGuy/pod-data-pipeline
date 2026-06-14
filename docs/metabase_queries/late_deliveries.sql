-- Late Deliveries
-- Card ID: 60
-- Collection: Root
-- Updated: 2026-06-06T11:14:18.987157Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  order_name AS "Order",
  shipping_country_name AS "Country",
  order_date :: date AS "Order Date",
  days_since_dispatch AS "Days Since Dispatch",
  carrier AS "Carrier",
  tracking_number AS "Tracking",
  fulfilment_status AS "Status",
  provider AS "Provider"
FROM
  order_fulfilment_status
WHERE
  brand_id = 'your_brand_id'
  AND is_late = TRUE
  AND override_flag = FALSE
UNION ALL
SELECT
  '✅ No late deliveries',
  '',
  NULL,
  NULL,
  '',
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
      AND is_late = TRUE
      AND override_flag = FALSE
  )
ORDER BY
  "Days Since Dispatch" DESC NULLS LAST;
