-- Expenses Breakdown
-- Card ID: 64
-- Collection: Root
-- Updated: 2026-06-06T14:46:47.904147Z
-- Extracted: 2026-06-14T10:36:25Z

WITH amex_expenses AS (
  SELECT
    DATE_TRUNC('month', transaction_date) :: date AS month,
    our_category,
    COALESCE(merchant_name, description) AS merchant,
    amount_gbp
  FROM
    amex_transactions
  WHERE
    brand_id = 'your_brand_id'
    AND amount_gbp > 0
    AND our_category NOT IN (
      'COGS_FULFILMENT',
      'ADS_META',
      'AMEX_PAYMENT_RECEIVED',
      'DRAWINGS_GUY',
      'DRAWINGS'
    )
    AND our_category IS NOT NULL
    AND transaction_date >= DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '2 months'
),
monzo_expenses AS (
  SELECT
    DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London') :: date AS month,
    our_category,
    description AS merchant,
    ABS(amount_gbp) AS amount_gbp
  FROM
    monzo_transactions
  WHERE
    brand_id = 'your_brand_id'
    AND amount_gbp < 0
    AND our_category NOT IN (
      'AMEX_PAYMENT',
      'DRAWINGS',
      'DRAWINGS_GUY',
      'INCOME_SHOPIFY',
      'INCOME_PAYPAL',
      'INCOME_KLARNA',
      'TAX_HMRC'
    )
    AND our_category IS NOT NULL
    AND (created_at AT TIME ZONE 'Europe/London') :: date >= DATE_TRUNC('month', CURRENT_DATE AT TIME ZONE 'Europe/London') - INTERVAL '2 months'
),
all_expenses AS (
  SELECT
    *
  FROM
    amex_expenses
  UNION ALL
  SELECT
    *
  FROM
    monzo_expenses
),
monthly_revenue AS (
  SELECT
    DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London') :: date AS month,
    SUM(revenue_gbp) AS revenue
  FROM
    orders
  WHERE
    brand_id = 'your_brand_id'
    AND financial_status NOT IN ('voided', 'refunded')
    AND created_at >= DATE_TRUNC('month', CURRENT_DATE AT TIME ZONE 'Europe/London') - INTERVAL '2 months'
  GROUP BY
    1
),
category_totals AS (
  SELECT
    e.month,
    e.our_category AS category,
    '  ' || e.our_category AS "Category / Merchant",
    COUNT(*) AS txns,
    ROUND(SUM(e.amount_gbp) :: numeric, 2) AS total_gbp,
    ROUND(
      (SUM(e.amount_gbp) / NULLIF(r.revenue, 0) * 100) :: numeric,
      2
    ) AS pct_revenue,
    1 AS row_type,
    e.our_category AS sort_category,
    0 AS sort_amount
  FROM
    all_expenses e
    LEFT JOIN monthly_revenue r ON r.month = e.month
  GROUP BY
    e.month,
    e.our_category,
    r.revenue
),
merchant_detail AS (
  SELECT
    e.month,
    e.our_category AS category,
    '    └ ' || e.merchant AS "Category / Merchant",
    COUNT(*) AS txns,
    ROUND(SUM(e.amount_gbp) :: numeric, 2) AS total_gbp,
    NULL :: numeric AS pct_revenue,
    2 AS row_type,
    e.our_category AS sort_category,
    ROUND(SUM(e.amount_gbp) :: numeric, 2) AS sort_amount
  FROM
    all_expenses e
  GROUP BY
    e.month,
    e.our_category,
    e.merchant
),
grand_total AS (
  SELECT
    e.month,
    'TOTAL' AS category,
    '★ TOTAL EXPENSES' AS "Category / Merchant",
    COUNT(*) AS txns,
    ROUND(SUM(e.amount_gbp) :: numeric, 2) AS total_gbp,
    ROUND(
      (SUM(e.amount_gbp) / NULLIF(r.revenue, 0) * 100) :: numeric,
      2
    ) AS pct_revenue,
    0 AS row_type,
    'AAAA' AS sort_category,
    0 AS sort_amount
  FROM
    all_expenses e
    LEFT JOIN monthly_revenue r ON r.month = e.month
  GROUP BY
    e.month,
    r.revenue
)
SELECT
  TO_CHAR(month, 'Mon YYYY') AS "Month",
  "Category / Merchant",
  txns AS "Txns",
  CONCAT('£', total_gbp) AS "Total £",
  CASE
    WHEN pct_revenue IS NOT NULL THEN CONCAT(pct_revenue, '%')
    ELSE ''
  END AS "% Revenue"
FROM
  (
    SELECT
      *
    FROM
      grand_total
    UNION ALL
    SELECT
      *
    FROM
      category_totals
    UNION ALL
    SELECT
      *
    FROM
      merchant_detail
  ) combined
ORDER BY
  month DESC,
  sort_category ASC,
  row_type ASC,
  sort_amount DESC;
