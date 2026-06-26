-- =============================================================================
-- Migration v8.32 — order_fulfilment_status.status_summary: drop the
--                   Shopify-fulfilled = delivered short-circuit
-- =============================================================================
-- Same logical bug as v8.29 fixed in `is_late` and `days_since_dispatch`:
-- Shopify marks fulfilled at *dispatch*, not delivery. The status_summary
-- CASE was still mapping that to 'delivered', so a fulfilled-by-Shopify
-- order with dispatched_at >7d and no delivered_at would simultaneously
-- show status_summary='delivered' AND is_late=true depending on which
-- column a downstream dashboard read.
--
-- This migration drops the offending branch. The fall-through
--   WHEN f.dispatched_at IS NOT NULL THEN 'in_transit'
-- handles fulfilled-at-Shopify-but-no-provider-delivery-confirmation
-- correctly.
--
-- Care points: any Metabase query that consumes `status_summary` for
-- delivery-state reporting will see fulfilled-but-not-actually-delivered
-- orders shift from 'delivered' to 'in_transit'. Audit before applying
-- in environments where that distinction drives anything downstream
-- (e.g. revenue recognition tied to delivered state).
-- =============================================================================

CREATE OR REPLACE VIEW order_fulfilment_status AS
 SELECT o.order_name,
    o.brand_id,
    o.created_at::date AS order_date,
    o.revenue_gbp,
    o.cogs_gbp,
    o.financial_status,
    o.refund_amount_gbp,
    o.shopify_fulfillment_status,
    o.cancelled_at,
    o.cancel_reason,
    o.payment_gateway,
    o.shipping_country_code,
    o.shipping_country_name,
    f.provider,
    f.provider_order_id,
    f.fulfilment_status,
    f.is_cancelled,
    f.tracking_number,
    f.tracking_url,
    f.carrier,
    f.dispatched_at,
    f.estimated_delivery_at,
    f.delivered_at,
    f.hours_to_dispatch,
    f.hours_to_delivery,
    f.fulfillment_country,
    CASE
      WHEN f.dispatched_at IS NOT NULL
       AND f.delivered_at  IS NULL
       AND (f.fulfilment_status::text <> ALL (ARRAY['delivered','fulfilled','shipment_delivered']::text[]))
       AND o.cancelled_at IS NULL
      THEN EXTRACT(day FROM now() - f.dispatched_at)::integer
      ELSE NULL::integer
    END AS days_since_dispatch,
    7 AS delivery_threshold_days,
    CASE
      WHEN o.cancelled_at IS NOT NULL                                                THEN false
      WHEN o.financial_status::text = ANY (ARRAY['refunded','voided']::text[])       THEN false
      WHEN f.is_cancelled = true                                                     THEN false
      WHEN f.fulfilment_status::text = ANY (ARRAY['returned','not_connected','canceled','cancelled']::text[]) THEN false
      WHEN f.delivered_at IS NOT NULL                                                THEN false
      WHEN f.fulfilment_status::text = ANY (ARRAY['delivered','fulfilled','shipment_delivered']::text[])       THEN false
      WHEN f.dispatched_at IS NULL                                                   THEN false
      ELSE EXTRACT(day FROM now() - f.dispatched_at)::integer > 7
    END AS is_late,
    CASE
      WHEN o.cancelled_at IS NOT NULL                                                THEN 'cancelled'::character varying
      WHEN f.is_cancelled = true                                                     THEN 'cancelled'::character varying
      WHEN f.fulfilment_status::text = 'returned'                                    THEN 'returned'::character varying
      WHEN f.fulfilment_status::text = 'returned_resolved'                           THEN 'returned_resolved'::character varying
      WHEN f.fulfilment_status::text = 'not_connected'                               THEN 'not_connected'::character varying
      WHEN f.delivered_at IS NOT NULL                                                THEN 'delivered'::character varying
      WHEN f.fulfilment_status::text = ANY (ARRAY['delivered','fulfilled','shipment_delivered']::text[]) THEN 'delivered'::character varying
      -- v8.32: removed the `WHEN o.shopify_fulfillment_status = ANY(['fulfilled','partial']) THEN 'delivered'`
      -- branch that was here. Shopify-fulfilled means dispatched, not delivered.
      -- Falls through to the dispatched-at-but-not-delivered branch below.
      WHEN f.dispatched_at IS NOT NULL                                               THEN 'in_transit'::character varying
      WHEN f.fulfilment_status::text = 'in_production'                               THEN 'in_production'::character varying
      WHEN f.fulfilment_status::text = 'printed'                                     THEN 'printed'::character varying
      WHEN f.fulfilment_status::text = 'passed'                                      THEN 'passed'::character varying
      WHEN f.fulfilment_status::text = 'pending_approval'                            THEN 'pending_approval'::character varying
      WHEN f.fulfilment_status::text = 'sent_to_production'                          THEN 'in_production'::character varying
      WHEN o.fulfillment_match_status::text = 'unmatched'                            THEN 'unmatched'::character varying
      ELSE COALESCE(f.fulfilment_status, 'unknown'::character varying)
    END AS status_summary,
    o.fulfillment_match_status,
    o.fulfillment_provider,
    o.fulfillment_order_id,
    f.dispatch_alert_sent,
    f.delivery_alert_sent,
    f.override_flag
   FROM orders o
     LEFT JOIN fulfilments f ON f.shopify_order_id::text = o.order_id::text
  WHERE o.brand_id::text = 'your_brand_id'::text
  ORDER BY o.created_at DESC;
