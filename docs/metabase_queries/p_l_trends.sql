-- P&L Trends
-- Card ID: 54
-- Collection: Root
-- Updated: 2026-05-31T12:17:19.109775Z
-- Extracted: 2026-06-14T10:36:25Z

WITH klaviyo AS (
  SELECT
    DATE_TRUNC('month', date) :: date AS month,
    ROUND(
      SUM(
        CASE
          WHEN campaign_type = 'flow' THEN revenue_attributed
          WHEN campaign_type = 'campaign' THEN revenue_attributed * 0.5
          ELSE 0
        END
      ) :: numeric,
      2
    ) AS email_revenue
  FROM
    email_campaigns
  WHERE
    brand_id = 'your_brand_id'
  GROUP BY
    1
),
returning_customers AS (
  SELECT
    DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London') :: date AS month,
    ROUND(
      (
        COUNT(
          DISTINCT CASE
            WHEN is_new_customer = false THEN order_id
          END
        ) :: numeric / NULLIF(COUNT(DISTINCT order_id), 0) * 100
      ) :: numeric,
      1
    ) AS returning_pct
  FROM
    orders
  WHERE
    brand_id = 'your_brand_id'
    AND financial_status NOT IN ('voided', 'refunded')
  GROUP BY
    1
)
SELECT
  TO_CHAR(p.month, 'Mon YYYY') AS "Month",
  p.orders AS "Orders",
  CONCAT('£', p.aov) AS "AOV",
  CONCAT('£', p.gross_revenue) AS "Revenue",
  CONCAT(
    ROUND(
      (p.cogs_orders / NULLIF(p.gross_revenue, 0) * 100) :: numeric,
      1
    ),
    '%'
  ) AS "COGS%",
  CONCAT(p.gross_margin_pct, '%') AS "GP%",
  CONCAT(
    ROUND(
      (p.meta_spend / NULLIF(p.gross_revenue, 0) * 100) :: numeric,
      1
    ),
    '%'
  ) AS "Meta%",
  CONCAT(p.mer, 'x') AS "MER",
  CONCAT(p.after_meta_pct, '%') AS "After Meta%",
  CONCAT(
    ROUND(
      (p.payment_fees / NULLIF(p.gross_revenue, 0) * 100) :: numeric,
      1
    ),
    '%'
  ) AS "Fees%",
  CONCAT(
    ROUND(
      (p.total_overheads / NULLIF(p.gross_revenue, 0) * 100) :: numeric,
      1
    ),
    '%'
  ) AS "OH%",
  CONCAT('£', p.operating_profit) AS "Op Profit",
  CONCAT(p.operating_margin_pct, '%') AS "Op Margin%",
  CONCAT('£', p.drawings) AS "Drawings",
  CONCAT('£', p.net_cash) AS "Net Cash",
  CONCAT(p.net_cash_pct, '%') AS "Net%",
  CONCAT(r.returning_pct, '%') AS "Returning%",
  CONCAT('£', COALESCE(k.email_revenue, 0)) AS "Email Rev",
  CONCAT(
    ROUND(
      (
        COALESCE(k.email_revenue, 0) / NULLIF(p.gross_revenue, 0) * 100
      ) :: numeric,
      1
    ),
    '%'
  ) AS "Email%",
  CONCAT(p.reconciliation_gap_pct, '%') AS "Recon Gap%"
FROM
  v_pl_monthly p
  LEFT JOIN returning_customers r ON r.month = p.month
  LEFT JOIN klaviyo k ON k.month = p.month
ORDER BY
  p.month DESC;
