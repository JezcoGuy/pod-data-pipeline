-- =============================================================================
-- Migration v8.30 — v_pl_monthly two-bug fix
-- =============================================================================
-- Issue 1: gross_revenue included fully-refunded orders, so Shopify's
--          reported "sales" figure disagreed with our gross_revenue.
--          Fix: exclude financial_status IN ('refunded','voided') from
--          the sales CTE.
--
-- Issue 2: refunds were bucketed by refunded_at (when the refund was
--          processed) instead of by the order's created_at (when the
--          sale was originally made). That meant a December order
--          refunded in January would inflate January's refund column
--          and disagree with Shopify's "Returns" report which attributes
--          refunds to the original sale month.
--          Fix: bucket refunds by orders.created_at AT TIME ZONE
--          'Europe/London' — same window as the sales CTE.
--
-- Untouched: cogs, amex, meta_spend, monzo, payouts CTEs and all
-- downstream maths. This is the smallest surgical change that addresses
-- the two reported issues; everything else is unchanged.
-- =============================================================================

CREATE OR REPLACE VIEW v_pl_monthly AS
 WITH sales AS (
         SELECT date_trunc('month'::text, (orders.created_at AT TIME ZONE 'Europe/London'::text))::date AS month,
            count(*) AS orders,
            avg(orders.revenue_gbp) AS aov,
            sum(orders.revenue_gbp) AS gross_revenue,
            sum(COALESCE(orders.total_payment_fees, 0::numeric)) AS payment_fees
           FROM orders
          WHERE orders.brand_id::text = 'your_brand_id'::text
            -- v8.30: exclude fully-refunded / voided orders so gross_revenue
            -- matches Shopify's reported sales. Partially-refunded orders
            -- stay in (their refund portion is netted out by the refunds CTE).
            AND (orders.financial_status::text <> ALL (ARRAY['refunded'::character varying, 'voided'::character varying]::text[]))
          GROUP BY (date_trunc('month'::text, (orders.created_at AT TIME ZONE 'Europe/London'::text))::date)
        ), refunds AS (
         SELECT
            -- v8.30: bucket by orders.created_at (the sale month), not
            -- refunded_at (the refund-processing month). Matches how
            -- Shopify's Returns report attributes refunds to the
            -- original sale.
            date_trunc('month'::text, (orders.created_at AT TIME ZONE 'Europe/London'::text))::date AS month,
            sum(COALESCE(orders.refund_amount_gbp, 0::numeric)) AS refunds
           FROM orders
          WHERE orders.brand_id::text = 'your_brand_id'::text
            AND (orders.financial_status::text = ANY (ARRAY['refunded'::character varying, 'partially_refunded'::character varying]::text[]))
            AND orders.refunded_at IS NOT NULL
          GROUP BY (date_trunc('month'::text, (orders.created_at AT TIME ZONE 'Europe/London'::text))::date)
        ), cogs AS (
         SELECT date_trunc('month'::text, (v_variant_sales.order_created_at AT TIME ZONE 'Europe/London'::text))::date AS month,
            sum(COALESCE(v_variant_sales.line_cogs_gbp, 0::numeric)) AS cogs_orders
           FROM v_variant_sales
          WHERE v_variant_sales.brand_id::text = 'your_brand_id'::text
          GROUP BY (date_trunc('month'::text, (v_variant_sales.order_created_at AT TIME ZONE 'Europe/London'::text))::date)
        ), amex AS (
         SELECT date_trunc('month'::text, amex_transactions.transaction_date::timestamp with time zone)::date AS month,
            sum(amex_transactions.amount_gbp) FILTER (WHERE amex_transactions.our_category::text = 'COGS_FULFILMENT'::text) AS cogs_amex,
            sum(amex_transactions.amount_gbp) FILTER (WHERE amex_transactions.our_category IS NOT NULL AND (amex_transactions.our_category::text <> ALL (ARRAY['AMEX_PAYMENT_RECEIVED'::character varying, 'DRAWINGS'::character varying, 'DRAWINGS_GUY'::character varying, 'ADS_META'::character varying, 'COGS_FULFILMENT'::character varying]::text[]))) AS amex_overheads
           FROM amex_transactions
          WHERE amex_transactions.brand_id::text = 'your_brand_id'::text
          GROUP BY (date_trunc('month'::text, amex_transactions.transaction_date::timestamp with time zone)::date)
        ), meta_spend_cte AS (
         SELECT COALESCE(a.month, m.month) AS month,
                CASE
                    WHEN COALESCE(a.amex_meta, 0::numeric) > 0::numeric THEN a.amex_meta
                    ELSE COALESCE(m.ads_meta, 0::numeric)
                END AS meta_spend
           FROM ( SELECT date_trunc('month'::text, amex_transactions.transaction_date::timestamp with time zone)::date AS month,
                    round(sum(amex_transactions.amount_gbp), 2) AS amex_meta
                   FROM amex_transactions
                  WHERE amex_transactions.brand_id::text = 'your_brand_id'::text AND amex_transactions.our_category::text = 'ADS_META'::text AND amex_transactions.amount_gbp > 0::numeric
                  GROUP BY (date_trunc('month'::text, amex_transactions.transaction_date::timestamp with time zone)::date)) a
             FULL JOIN ( SELECT date_trunc('month'::text, (ad_campaigns.date AT TIME ZONE 'Europe/London'::text))::date AS month,
                    round(sum(ad_campaigns.spend_gbp), 2) AS ads_meta
                   FROM ad_campaigns
                  WHERE ad_campaigns.brand_id::text = 'your_brand_id'::text
                  GROUP BY (date_trunc('month'::text, (ad_campaigns.date AT TIME ZONE 'Europe/London'::text))::date)) m ON m.month = a.month
        ), monzo AS (
         SELECT date_trunc('month'::text, (monzo_transactions.created_at AT TIME ZONE 'Europe/London'::text))::date AS month,
            abs(sum(monzo_transactions.amount_gbp) FILTER (WHERE monzo_transactions.amount_gbp < 0::numeric AND monzo_transactions.our_category IS NOT NULL AND monzo_transactions.our_category::text !~~ 'INCOME_%'::text AND (monzo_transactions.our_category::text <> ALL (ARRAY['AMEX_PAYMENT'::character varying, 'DRAWINGS'::character varying, 'DRAWINGS_GUY'::character varying, 'TAX_HMRC'::character varying]::text[])))) AS monzo_overheads,
            abs(sum(monzo_transactions.amount_gbp) FILTER (WHERE monzo_transactions.our_category::text = ANY (ARRAY['DRAWINGS'::character varying, 'DRAWINGS_GUY'::character varying]::text[]))) AS drawings,
            abs(sum(monzo_transactions.amount_gbp) FILTER (WHERE monzo_transactions.our_category::text = 'TAX_HMRC'::text)) AS tax,
            sum(monzo_transactions.amount_gbp) AS net_monzo_movement
           FROM monzo_transactions
          WHERE monzo_transactions.brand_id::text = 'your_brand_id'::text
          GROUP BY (date_trunc('month'::text, (monzo_transactions.created_at AT TIME ZONE 'Europe/London'::text))::date)
        ), payouts AS (
         SELECT date_trunc('month'::text, shopify_payouts.payout_date - '2 days'::interval)::date AS month,
            sum(shopify_payouts.amount_gbp) FILTER (WHERE shopify_payouts.status::text = ANY (ARRAY['in_transit'::character varying, 'scheduled'::character varying]::text[])) AS pending_payouts
           FROM shopify_payouts
          WHERE shopify_payouts.brand_id::text = 'your_brand_id'::text
          GROUP BY (date_trunc('month'::text, shopify_payouts.payout_date - '2 days'::interval)::date)
        ), months AS (
         SELECT sales.month FROM sales
        UNION
         SELECT refunds.month FROM refunds
        UNION
         SELECT cogs.month FROM cogs
        UNION
         SELECT amex.month FROM amex
        UNION
         SELECT meta_spend_cte.month FROM meta_spend_cte
        UNION
         SELECT monzo.month FROM monzo
        UNION
         SELECT payouts.month FROM payouts
        ), base AS (
         SELECT m.month,
            COALESCE(s.orders, 0::bigint) AS orders,
            COALESCE(s.aov, 0::numeric) AS aov,
            COALESCE(s.gross_revenue, 0::numeric) AS gross_revenue,
            COALESCE(r.refunds, 0::numeric) AS refunds,
            COALESCE(c.cogs_orders, 0::numeric) AS cogs_orders,
            COALESCE(a.cogs_amex, 0::numeric) AS cogs_amex,
            COALESCE(ms.meta_spend, 0::numeric) AS meta_spend,
            COALESCE(s.payment_fees, 0::numeric) AS payment_fees,
            COALESCE(a.amex_overheads, 0::numeric) AS amex_overheads,
            COALESCE(mz.monzo_overheads, 0::numeric) AS monzo_overheads,
            COALESCE(mz.drawings, 0::numeric) AS drawings,
            COALESCE(mz.tax, 0::numeric) AS tax,
            COALESCE(mz.net_monzo_movement, 0::numeric) AS net_monzo_movement,
            COALESCE(p.pending_payouts, 0::numeric) AS pending_payouts
           FROM months m
             LEFT JOIN sales s ON s.month = m.month
             LEFT JOIN refunds r ON r.month = m.month
             LEFT JOIN cogs c ON c.month = m.month
             LEFT JOIN amex a ON a.month = m.month
             LEFT JOIN meta_spend_cte ms ON ms.month = m.month
             LEFT JOIN monzo mz ON mz.month = m.month
             LEFT JOIN payouts p ON p.month = m.month
          WHERE m.month IS NOT NULL
        )
 SELECT month,
    orders,
    round(aov, 2) AS aov,
    round(gross_revenue, 2) AS gross_revenue,
    round(refunds, 2) AS refunds,
    round(gross_revenue - refunds, 2) AS net_revenue,
    round(cogs_orders, 2) AS cogs_orders,
    round(cogs_amex, 2) AS cogs_amex,
    round(gross_revenue - refunds - cogs_orders, 2) AS gross_profit,
    round((gross_revenue - refunds - cogs_orders) / NULLIF(gross_revenue, 0::numeric) * 100::numeric, 2) AS gross_margin_pct,
    round(meta_spend, 2) AS meta_spend,
    round(gross_revenue / NULLIF(meta_spend, 0::numeric), 2) AS mer,
    round(gross_revenue - refunds - cogs_orders - meta_spend, 2) AS after_meta,
    round((gross_revenue - refunds - cogs_orders - meta_spend) / NULLIF(gross_revenue, 0::numeric) * 100::numeric, 2) AS after_meta_pct,
    round(payment_fees, 2) AS payment_fees,
    round(amex_overheads, 2) AS amex_overheads,
    round(monzo_overheads, 2) AS monzo_overheads,
    round(payment_fees + amex_overheads + monzo_overheads, 2) AS total_overheads,
    round(gross_revenue - refunds - cogs_orders - meta_spend - payment_fees - amex_overheads - monzo_overheads, 2) AS operating_profit,
    round((gross_revenue - refunds - cogs_orders - meta_spend - payment_fees - amex_overheads - monzo_overheads) / NULLIF(gross_revenue, 0::numeric) * 100::numeric, 2) AS operating_margin_pct,
    round(tax, 2) AS tax,
    round(drawings, 2) AS drawings,
    round(gross_revenue - refunds - cogs_orders - meta_spend - payment_fees - amex_overheads - monzo_overheads - tax - drawings, 2) AS net_cash,
    round((gross_revenue - refunds - cogs_orders - meta_spend - payment_fees - amex_overheads - monzo_overheads - tax - drawings) / NULLIF(gross_revenue, 0::numeric) * 100::numeric, 2) AS net_cash_pct,
    round(net_monzo_movement, 2) AS monzo_net_movement,
    round(net_monzo_movement / NULLIF(gross_revenue, 0::numeric) * 100::numeric, 2) AS monzo_net_pct,
    round(pending_payouts, 2) AS pending_payouts,
    round((gross_revenue - refunds - cogs_orders - meta_spend - payment_fees - amex_overheads - monzo_overheads - tax - drawings - net_monzo_movement) / NULLIF(gross_revenue, 0::numeric) * 100::numeric, 2) AS reconciliation_gap_pct
   FROM base
  ORDER BY month DESC;
