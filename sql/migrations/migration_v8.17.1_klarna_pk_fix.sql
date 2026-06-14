-- Migration v8.17.1 — fix klarna_transactions PK to allow NULL capture_id
-- ------------------------------------------------------------------------
-- Discovered during the first backfill: Klarna RETURN rows (and similar
-- post-capture lifecycle events) do not carry a capture_id — only an
-- order_id. The original composite PK (capture_id, type, detailed_type)
-- breaks because capture_id is NOT NULL on it.
--
-- Fix: synthetic BIGSERIAL PK, plus an expression-based UNIQUE index on
-- COALESCE(capture_id, klarna_order_id) + type + detailed_type. The
-- COALESCE gives us natural-key uniqueness for both SALE/FEE rows (which
-- have capture_id) and RETURN/REVERSAL rows (which only have klarna_order_id).
-- ON CONFLICT in the sync script references the same expression list.

ALTER TABLE klarna_transactions DROP CONSTRAINT klarna_transactions_pkey;
ALTER TABLE klarna_transactions ALTER COLUMN capture_id DROP NOT NULL;
ALTER TABLE klarna_transactions ADD COLUMN id BIGSERIAL PRIMARY KEY;

CREATE UNIQUE INDEX klarna_transactions_natural_key
    ON klarna_transactions (
        COALESCE(capture_id, klarna_order_id),
        type,
        detailed_type
    );
