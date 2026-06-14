-- Migration v8.20 — v_pl_monthly P&L roll-up
-- --------------------------------------------
-- One row per calendar month (Europe/London) summarising the full P&L from
-- five sources:
--   orders                — gross_revenue, aov, payment_fees (by created_at)
--                          refunds (by refunded_at, refunded/partially_refunded rows)
--   v_variant_sales       — cogs_orders (line_cogs_gbp, by order_created_at)
--   amex_transactions     — meta_spend, cogs_amex, amex_overheads (by transaction_date, no tz)
--   monzo_transactions    — monzo_overheads, drawings, tax, net_monzo_movement (by created_at)
--   shopify_payouts       — pending_payouts (status in_transit/scheduled, by payout_date)
--
-- All timestamptz sources are converted to Europe/London before month truncation
-- so a Shopify order placed at 23:30 UTC on the 31st (which is 00:30 the next
-- day in London) doesn't fall into the wrong month.
--
-- Operating-profit chain:
--   net_revenue     = gross_revenue - refunds
--   gross_profit    = net_revenue - cogs_orders                          (orders COGS = primary)
--   after_meta      = gross_profit - meta_spend
--   total_overheads = payment_fees + amex_overheads + monzo_overheads
--   operating_profit= after_meta - total_overheads
--   net_cash        = operating_profit - tax - drawings
--
-- Reconciliation gap compares the P&L's predicted net_cash to the actual
-- Monzo net movement — a 5-20% gap is usually Amex 30-day timing lag.

CREATE OR REPLACE VIEW v_pl_monthly AS
WITH
  sales AS (
    SELECT
      DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London')::date AS month,
      COUNT(*)                            AS orders,
      AVG(revenue_gbp)                    AS aov,
      SUM(revenue_gbp)                    AS gross_revenue,
      SUM(COALESCE(total_payment_fees,0)) AS payment_fees
    FROM orders
    WHERE brand_id = 'your_brand_id'
    GROUP BY 1
  ),
  refunds AS (
    SELECT
      DATE_TRUNC('month', refunded_at AT TIME ZONE 'Europe/London')::date AS month,
      SUM(COALESCE(refund_amount_gbp, 0)) AS refunds
    FROM orders
    WHERE brand_id = 'your_brand_id'
      AND financial_status IN ('refunded', 'partially_refunded')
      AND refunded_at IS NOT NULL
    GROUP BY 1
  ),
  cogs AS (
    SELECT
      DATE_TRUNC('month', order_created_at AT TIME ZONE 'Europe/London')::date AS month,
      SUM(COALESCE(line_cogs_gbp, 0)) AS cogs_orders
    FROM v_variant_sales
    WHERE brand_id = 'your_brand_id'
    GROUP BY 1
  ),
  amex AS (
    SELECT
      DATE_TRUNC('month', transaction_date)::date AS month,
      SUM(amount_gbp) FILTER (WHERE our_category = 'ADS_META')        AS meta_spend,
      SUM(amount_gbp) FILTER (WHERE our_category = 'COGS_FULFILMENT') AS cogs_amex,
      -- Real-overhead spend on Amex: exclude transfers, drawings, the lines
      -- that already have their own column (meta + cogs), and uncategorised.
      SUM(amount_gbp) FILTER (
        WHERE our_category IS NOT NULL
          AND our_category NOT IN ('AMEX_PAYMENT_RECEIVED',
                                   'DRAWINGS', 'DRAWINGS_GUY',
                                   'ADS_META', 'COGS_FULFILMENT')
      ) AS amex_overheads
    FROM amex_transactions
    WHERE brand_id = 'your_brand_id'
    GROUP BY 1
  ),
  monzo AS (
    SELECT
      DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London')::date AS month,
      -- monzo_overheads = ABS of outbound spend, excluding income,
      -- AMEX_PAYMENT (transfer), drawings (own column), tax (own column),
      -- and uncategorised needs_review rows.
      ABS(SUM(amount_gbp) FILTER (
        WHERE amount_gbp < 0
          AND our_category IS NOT NULL
          AND our_category NOT LIKE 'INCOME_%'
          AND our_category NOT IN ('AMEX_PAYMENT',
                                   'DRAWINGS', 'DRAWINGS_GUY',
                                   'TAX_HMRC')
      )) AS monzo_overheads,
      ABS(SUM(amount_gbp) FILTER (WHERE our_category IN ('DRAWINGS', 'DRAWINGS_GUY'))) AS drawings,
      ABS(SUM(amount_gbp) FILTER (WHERE our_category = 'TAX_HMRC'))                   AS tax,
      SUM(amount_gbp)                                                                  AS net_monzo_movement
    FROM monzo_transactions
    WHERE brand_id = 'your_brand_id'
    GROUP BY 1
  ),
  payouts AS (
    -- Pending payouts represent sales already EARNED but not yet landed in
    -- the bank. We bucket by (payout_date - 2 days) to approximate the
    -- charge month, matching Shopify Payments' standard T+2 settlement.
    SELECT
      DATE_TRUNC('month', (payout_date - INTERVAL '2 days'))::date AS month,
      SUM(amount_gbp) FILTER (WHERE status IN ('in_transit', 'scheduled')) AS pending_payouts
    FROM shopify_payouts
    WHERE brand_id = 'your_brand_id'
    GROUP BY 1
  ),
  months AS (
    SELECT month FROM sales
    UNION SELECT month FROM refunds
    UNION SELECT month FROM cogs
    UNION SELECT month FROM amex
    UNION SELECT month FROM monzo
    UNION SELECT month FROM payouts
  ),
  base AS (
    SELECT
      m.month,
      COALESCE(s.orders, 0)               AS orders,
      COALESCE(s.aov, 0)                  AS aov,
      COALESCE(s.gross_revenue, 0)        AS gross_revenue,
      COALESCE(r.refunds, 0)              AS refunds,
      COALESCE(c.cogs_orders, 0)          AS cogs_orders,
      COALESCE(a.cogs_amex, 0)            AS cogs_amex,
      COALESCE(a.meta_spend, 0)           AS meta_spend,
      COALESCE(s.payment_fees, 0)         AS payment_fees,
      COALESCE(a.amex_overheads, 0)       AS amex_overheads,
      COALESCE(mz.monzo_overheads, 0)     AS monzo_overheads,
      COALESCE(mz.drawings, 0)            AS drawings,
      COALESCE(mz.tax, 0)                 AS tax,
      COALESCE(mz.net_monzo_movement, 0)  AS net_monzo_movement,
      COALESCE(p.pending_payouts, 0)      AS pending_payouts
    FROM months m
    LEFT JOIN sales   s  ON s.month  = m.month
    LEFT JOIN refunds r  ON r.month  = m.month
    LEFT JOIN cogs    c  ON c.month  = m.month
    LEFT JOIN amex    a  ON a.month  = m.month
    LEFT JOIN monzo   mz ON mz.month = m.month
    LEFT JOIN payouts p  ON p.month  = m.month
    WHERE m.month IS NOT NULL
  )
SELECT
  month,
  orders,
  ROUND(aov::numeric, 2)                                                         AS aov,
  ROUND(gross_revenue::numeric, 2)                                               AS gross_revenue,
  ROUND(refunds::numeric, 2)                                                     AS refunds,
  ROUND((gross_revenue - refunds)::numeric, 2)                                   AS net_revenue,

  ROUND(cogs_orders::numeric, 2)                                                 AS cogs_orders,
  ROUND(cogs_amex::numeric, 2)                                                   AS cogs_amex,
  ROUND((gross_revenue - refunds - cogs_orders)::numeric, 2)                     AS gross_profit,
  ROUND(((gross_revenue - refunds - cogs_orders)
         / NULLIF(gross_revenue, 0) * 100)::numeric, 2)                          AS gross_margin_pct,

  ROUND(meta_spend::numeric, 2)                                                  AS meta_spend,
  ROUND((gross_revenue / NULLIF(meta_spend, 0))::numeric, 2)                     AS mer,
  ROUND((gross_revenue - refunds - cogs_orders - meta_spend)::numeric, 2)        AS after_meta,
  ROUND(((gross_revenue - refunds - cogs_orders - meta_spend)
         / NULLIF(gross_revenue, 0) * 100)::numeric, 2)                          AS after_meta_pct,

  ROUND(payment_fees::numeric, 2)                                                AS payment_fees,
  ROUND(amex_overheads::numeric, 2)                                              AS amex_overheads,
  ROUND(monzo_overheads::numeric, 2)                                             AS monzo_overheads,
  ROUND((payment_fees + amex_overheads + monzo_overheads)::numeric, 2)           AS total_overheads,

  ROUND((gross_revenue - refunds - cogs_orders - meta_spend
         - payment_fees - amex_overheads - monzo_overheads)::numeric, 2)         AS operating_profit,
  ROUND(((gross_revenue - refunds - cogs_orders - meta_spend
          - payment_fees - amex_overheads - monzo_overheads)
         / NULLIF(gross_revenue, 0) * 100)::numeric, 2)                          AS operating_margin_pct,

  ROUND(tax::numeric, 2)                                                         AS tax,
  ROUND(drawings::numeric, 2)                                                    AS drawings,
  ROUND((gross_revenue - refunds - cogs_orders - meta_spend
         - payment_fees - amex_overheads - monzo_overheads
         - tax - drawings)::numeric, 2)                                          AS net_cash,
  ROUND(((gross_revenue - refunds - cogs_orders - meta_spend
          - payment_fees - amex_overheads - monzo_overheads
          - tax - drawings)
         / NULLIF(gross_revenue, 0) * 100)::numeric, 2)                          AS net_cash_pct,

  ROUND(net_monzo_movement::numeric, 2)                                          AS monzo_net_movement,
  ROUND((net_monzo_movement / NULLIF(gross_revenue, 0) * 100)::numeric, 2)       AS monzo_net_pct,
  ROUND(pending_payouts::numeric, 2)                                             AS pending_payouts,

  ROUND((((gross_revenue - refunds - cogs_orders - meta_spend
           - payment_fees - amex_overheads - monzo_overheads
           - tax - drawings) - net_monzo_movement)
         / NULLIF(gross_revenue, 0) * 100)::numeric, 2)                          AS reconciliation_gap_pct
FROM base
ORDER BY month DESC;
