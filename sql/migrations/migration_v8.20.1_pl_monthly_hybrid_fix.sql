-- Migration v8.20.1 — v_pl_monthly hybrid Meta spend
-- ----------------------------------------------------
-- Previously v_pl_monthly read meta_spend from amex_transactions only.
-- Amex statements are uploaded manually at month-end so the CURRENT month
-- always showed £0 Meta spend → operating_profit wildly overstated until
-- the statement arrived.
--
-- This fix introduces a hybrid: prefer the Amex figure when present
-- (completed months — invoice-accurate), fall back to ad_campaigns.spend_gbp
-- otherwise (current month — Meta API live data, ~30 min lag).
--
-- Self-healing: when the month's Amex statement is ingested, amex_meta > 0
-- and the CASE flips back to Amex automatically. No re-ingest of historicals.
--
-- COGS and amex_overheads stay as-is per the brief — cogs_orders is the
-- primary COGS source and is always real-time; monzo_overheads already
-- covers current-month operational spend; only Meta needed the fallback.

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
    -- cogs_amex and amex_overheads stay Amex-only (informational / additive
    -- to other sources). meta_spend is computed in its own hybrid CTE below.
    SELECT
      DATE_TRUNC('month', transaction_date)::date AS month,
      SUM(amount_gbp) FILTER (WHERE our_category = 'COGS_FULFILMENT') AS cogs_amex,
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
  meta_spend_cte AS (
    -- Hybrid: prefer Amex (invoice truth), fall back to ad_campaigns (live).
    SELECT
      COALESCE(a.month, m.month) AS month,
      CASE
        WHEN COALESCE(a.amex_meta, 0) > 0 THEN a.amex_meta
        ELSE COALESCE(m.ads_meta, 0)
      END AS meta_spend
    FROM (
      SELECT
        DATE_TRUNC('month', transaction_date)::date AS month,
        ROUND(SUM(amount_gbp)::numeric, 2)          AS amex_meta
      FROM amex_transactions
      WHERE brand_id = 'your_brand_id'
        AND our_category = 'ADS_META'
        AND amount_gbp > 0
      GROUP BY 1
    ) a
    FULL OUTER JOIN (
      SELECT
        DATE_TRUNC('month', date AT TIME ZONE 'Europe/London')::date AS month,
        ROUND(SUM(spend_gbp)::numeric, 2)                            AS ads_meta
      FROM ad_campaigns
      WHERE brand_id = 'your_brand_id'
      GROUP BY 1
    ) m ON m.month = a.month
  ),
  monzo AS (
    SELECT
      DATE_TRUNC('month', created_at AT TIME ZONE 'Europe/London')::date AS month,
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
    -- T+2 approx: pending payouts show on the month they were earned, not
    -- the month they're scheduled to land.
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
    UNION SELECT month FROM meta_spend_cte           -- includes ad_campaigns months
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
      COALESCE(ms.meta_spend, 0)          AS meta_spend,
      COALESCE(s.payment_fees, 0)         AS payment_fees,
      COALESCE(a.amex_overheads, 0)       AS amex_overheads,
      COALESCE(mz.monzo_overheads, 0)     AS monzo_overheads,
      COALESCE(mz.drawings, 0)            AS drawings,
      COALESCE(mz.tax, 0)                 AS tax,
      COALESCE(mz.net_monzo_movement, 0)  AS net_monzo_movement,
      COALESCE(p.pending_payouts, 0)      AS pending_payouts
    FROM months m
    LEFT JOIN sales          s  ON s.month  = m.month
    LEFT JOIN refunds        r  ON r.month  = m.month
    LEFT JOIN cogs           c  ON c.month  = m.month
    LEFT JOIN amex           a  ON a.month  = m.month
    LEFT JOIN meta_spend_cte ms ON ms.month = m.month
    LEFT JOIN monzo          mz ON mz.month = m.month
    LEFT JOIN payouts        p  ON p.month  = m.month
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
