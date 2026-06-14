-- P&L Reconciliation
-- Card ID: 55
-- Collection: Root
-- Updated: 2026-05-31T12:32:51.689981Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  TO_CHAR(month, 'Mon YYYY') AS "Month",
  CONCAT('£', gross_revenue) AS "Revenue",
  CONCAT(net_cash_pct, '%') AS "P&L Net%",
  CONCAT(monzo_net_pct, '%') AS "Monzo Net%",
  CONCAT(reconciliation_gap_pct, '%') AS "Gap%",
  CASE
    WHEN ABS(reconciliation_gap_pct) > 20 THEN '🚨 Investigate'
    WHEN ABS(reconciliation_gap_pct) > 10 THEN '⚠️ Review'
    WHEN ABS(reconciliation_gap_pct) > 5 THEN '👀 Watch'
    ELSE '✅ Normal'
  END AS "Flag",
  ROUND(
    (
      SUM(net_cash) OVER (
        ORDER BY
          month
      ) / NULLIF(
        SUM(gross_revenue) OVER (
          ORDER BY
            month
        ),
        0
      ) * 100
    ) :: numeric,
    1
  ) AS "Cum P&L%",
  ROUND(
    (
      SUM(monzo_net_movement) OVER (
        ORDER BY
          month
      ) / NULLIF(
        SUM(gross_revenue) OVER (
          ORDER BY
            month
        ),
        0
      ) * 100
    ) :: numeric,
    1
  ) AS "Cum Monzo%"
FROM
  v_pl_monthly
WHERE
  month >= '2025-01-01'
  AND gross_revenue > 0
ORDER BY
  month DESC;
