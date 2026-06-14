-- P&L Daily
-- Card ID: 57
-- Collection: Root
-- Updated: 2026-06-04T08:12:16.871249Z
-- Extracted: 2026-06-14T10:36:25Z

WITH orders_data AS (
  SELECT
    (created_at AT TIME ZONE 'Europe/London') :: date AS day,
    COUNT(DISTINCT order_id) AS orders,
    SUM(line_items_count) AS items,
    ROUND(SUM(revenue_gbp) :: numeric, 2) AS revenue,
    ROUND(AVG(revenue_gbp) :: numeric, 2) AS aov,
    ROUND(
      SUM(
        CASE
          WHEN (created_at AT TIME ZONE 'Europe/London') :: date >= CURRENT_DATE - 1
          AND cogs_gbp = 0 THEN revenue_gbp * 0.421
          ELSE cogs_gbp
        END
      ) :: numeric,
      2
    ) AS cogs,
    ROUND(SUM(total_payment_fees) :: numeric, 2) AS fees,
    COUNT(
      DISTINCT CASE
        WHEN financial_status IN ('refunded', 'partially_refunded') THEN order_id
      END
    ) AS refunded_count
  FROM
    orders
  WHERE
    brand_id = 'your_brand_id'
    AND created_at >= NOW() - INTERVAL '30 days'
  GROUP BY
    1
),
daily_refunds AS (
  SELECT
    (created_at AT TIME ZONE 'Europe/London') :: date AS day,
    ROUND(SUM(refund_amount_gbp) :: numeric, 2) AS refund_value
  FROM
    orders
  WHERE
    brand_id = 'your_brand_id'
    AND created_at >= NOW() - INTERVAL '30 days'
    AND financial_status IN ('refunded', 'partially_refunded')
    AND refund_amount_gbp > 0
  GROUP BY
    1
),
daily_meta AS (
  SELECT
    (date AT TIME ZONE 'Europe/London') :: date AS day,
    ROUND(SUM(spend_gbp) :: numeric, 2) AS meta_spend
  FROM
    ad_campaigns
  WHERE
    brand_id = 'your_brand_id'
    AND date >= CURRENT_DATE - INTERVAL '30 days'
  GROUP BY
    1
)
SELECT
  TO_CHAR(o.day, 'Dy DD Mon YYYY') AS "Date",
  CONCAT('£', o.revenue) AS "Revenue",
  ROUND(o.revenue / NULLIF(COALESCE(m.meta_spend, 0), 0), 2) AS "MER",
  CONCAT('£', o.aov) AS "AOV",
  CONCAT('£', COALESCE(m.meta_spend, 0)) AS "Ad Spend",
  CONCAT(
    ROUND(
      (COALESCE(m.meta_spend, 0) / NULLIF(o.revenue, 0) * 100) :: numeric,
      1
    ),
    '%'
  ) AS "Ad%",
  CONCAT('£', o.cogs) AS "COGS",
  CONCAT(
    '£',
    ROUND(
      (o.revenue - o.cogs - COALESCE(m.meta_spend, 0) - o.fees) :: numeric,
      2
    )
  ) AS "£Op",
  CONCAT(
    ROUND(
      (
        (o.revenue - o.cogs - COALESCE(m.meta_spend, 0) - o.fees) / NULLIF(o.revenue, 0) * 100
      ) :: numeric,
      1
    ),
    '%'
  ) AS "Op%",
  CASE
    WHEN COALESCE(r.refund_value, 0) = 0 THEN '-'
    ELSE CONCAT('£', r.refund_value)
  END AS "Refunded"
FROM
  orders_data o
  LEFT JOIN daily_meta m ON m.day = o.day
  LEFT JOIN daily_refunds r ON r.day = o.day
WHERE
  o.revenue > 0
  AND o.day < CURRENT_DATE
ORDER BY
  o.day DESC;
