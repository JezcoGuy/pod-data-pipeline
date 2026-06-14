-- Current Month
-- Card ID: 56
-- Collection: Root
-- Updated: 2026-05-31T12:37:41.949736Z
-- Extracted: 2026-06-14T10:36:25Z

WITH m AS (
  SELECT
    *
  FROM
    v_pl_monthly
  WHERE
    month = DATE_TRUNC('month', CURRENT_DATE AT TIME ZONE 'Europe/London') :: date
  LIMIT
    1
)
SELECT
  section,
  line_item,
  amount,
  pct
FROM
  (
    SELECT
      1 AS sort,
      'REVENUE' AS section,
      'Gross Revenue' AS line_item,
      CONCAT('£', TO_CHAR(gross_revenue, 'FM999,999,990.00')) AS amount,
      '' AS pct
    FROM
      m
    UNION ALL
    SELECT
      2,
      'REVENUE',
      'Refunds',
      CONCAT('-£', TO_CHAR(refunds, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      3,
      'REVENUE',
      'Net Revenue',
      CONCAT('£', TO_CHAR(net_revenue, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      4,
      'COGS',
      'Fulfilment (Gelato/Printify)',
      CONCAT('-£', TO_CHAR(cogs_orders, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      5,
      'COGS',
      'Gross Profit',
      CONCAT('£', TO_CHAR(gross_profit, 'FM999,999,990.00')),
      CONCAT(gross_margin_pct, '%')
    FROM
      m
    UNION ALL
    SELECT
      6,
      'MARKETING',
      'Meta Ad Spend',
      CONCAT('-£', TO_CHAR(meta_spend, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      7,
      'MARKETING',
      'After Meta',
      CONCAT('£', TO_CHAR(after_meta, 'FM999,999,990.00')),
      CONCAT('MER: ', mer, 'x  |  ', after_meta_pct, '%')
    FROM
      m
    UNION ALL
    SELECT
      8,
      'FEES',
      'Payment Fees',
      CONCAT('-£', TO_CHAR(payment_fees, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      9,
      'OVERHEADS',
      'Amex Overheads',
      CONCAT('-£', TO_CHAR(amex_overheads, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      10,
      'OVERHEADS',
      'Monzo Overheads',
      CONCAT('-£', TO_CHAR(monzo_overheads, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      11,
      'OVERHEADS',
      'Total Overheads',
      CONCAT('-£', TO_CHAR(total_overheads, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      12,
      'OPERATING PROFIT',
      'Operating Profit',
      CONCAT('£', TO_CHAR(operating_profit, 'FM999,999,990.00')),
      CONCAT(operating_margin_pct, '%')
    FROM
      m
    UNION ALL
    SELECT
      13,
      'TAX',
      'HMRC Tax',
      CONCAT('-£', TO_CHAR(tax, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      14,
      'DRAWINGS',
      'Director Drawings',
      CONCAT('-£', TO_CHAR(drawings, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      15,
      'NET CASH',
      'Net Cash Contribution',
      CONCAT('£', TO_CHAR(net_cash, 'FM999,999,990.00')),
      CONCAT(net_cash_pct, '%')
    FROM
      m
    UNION ALL
    SELECT
      16,
      'RECONCILIATION',
      'Monzo Net Movement',
      CONCAT(
        '£',
        TO_CHAR(monzo_net_movement, 'FM999,999,990.00')
      ),
      CONCAT(monzo_net_pct, '%')
    FROM
      m
    UNION ALL
    SELECT
      17,
      'RECONCILIATION',
      'Pending Payouts',
      CONCAT('£', TO_CHAR(pending_payouts, 'FM999,999,990.00')),
      ''
    FROM
      m
    UNION ALL
    SELECT
      18,
      'RECONCILIATION',
      'Reconciliation Gap',
      '',
      CONCAT(reconciliation_gap_pct, '%')
    FROM
      m
  ) p
ORDER BY
  sort;
