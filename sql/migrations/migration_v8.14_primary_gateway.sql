-- Migration v8.14 — Normalised primary_gateway on orders
-- -------------------------------------------------------
-- The raw `payment_gateway` column on orders is whatever Shopify returned
-- in `payment_gateway_names`, comma-joined. For 64 multi-payment orders
-- this means values like 'shopify_payments,paypal' or 'paypal,Klarna' that
-- break GROUP BY rollups.
--
-- This migration adds `primary_gateway`, populates it with a normalised
-- single-value classification, and rewrites v_fee_by_payment_gateway to
-- group on it.
--
-- Normalisation priority (Klarna before PayPal before Shopify Payments
-- before manual) is set so that for a split-payment order, the "more
-- expensive / more notable" processor wins — Klarna is the rarest +
-- highest-fee, so it's the more interesting attribution.

ALTER TABLE orders ADD COLUMN IF NOT EXISTS primary_gateway VARCHAR(64);

UPDATE orders
SET primary_gateway = CASE
    WHEN LOWER(payment_gateway) LIKE '%klarna%'           THEN 'klarna'
    WHEN LOWER(payment_gateway) LIKE '%paypal%'           THEN 'paypal'
    WHEN LOWER(payment_gateway) LIKE '%shopify_payments%' THEN 'shopify_payments'
    WHEN LOWER(payment_gateway) LIKE '%manual%'           THEN 'manual'
    ELSE payment_gateway
END
WHERE brand_id = 'your_brand_id';

CREATE INDEX IF NOT EXISTS idx_orders_primary_gateway
    ON orders (brand_id, primary_gateway);

-- Rebuild the fee-by-gateway view against the normalised column
CREATE OR REPLACE VIEW v_fee_by_payment_gateway AS
SELECT
    o.primary_gateway                                                               AS payment_gateway,
    COUNT(DISTINCT o.order_id)                                                      AS orders,
    ROUND(SUM(o.revenue_gbp)::numeric, 2)                                           AS gross_revenue,
    ROUND(SUM(o.shopify_fee_gbp)::numeric, 2)                                       AS total_fees,
    ROUND(AVG(o.shopify_fee_pct)::numeric, 4)                                       AS avg_fee_pct,
    ROUND(AVG(o.revenue_gbp)::numeric, 2)                                           AS avg_order_value
FROM orders o
WHERE o.brand_id = 'your_brand_id'
  AND o.financial_status NOT IN ('voided', 'refunded')
  AND o.shopify_fee_gbp  IS NOT NULL
  AND o.primary_gateway  IS NOT NULL
  AND o.primary_gateway  != ''
GROUP BY o.primary_gateway
ORDER BY total_fees DESC;
