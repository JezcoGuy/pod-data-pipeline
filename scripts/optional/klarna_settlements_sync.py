"""
klarna_settlements_sync.py
==========================
Ingest Klarna Settlements API data into klarna_payouts + klarna_transactions
and stamp orders.klarna_fee_gbp on matched GBP orders.

Klarna model in a nutshell:
- /settlements/v1/payouts returns weekly payout-level totals (aggregates)
- /settlements/v1/transactions returns per-line items where a single sale
  produces THREE rows: SALE + FEE/PURCHASE_FEE_PERCENTAGE + FEE/PURCHASE_FEE_FIXED
- All amounts are in MINOR UNITS (pence); we divide by 100 at ingestion to
  match the rest of the schema
- The join key to Shopify is `merchant_reference2` (bare r... token) which
  matches `shopify_order_transactions.receipt ->> 'payment_id'` for Klarna
  bridge rows

Fee aggregation per order: SUM(amount) WHERE type='FEE' GROUP BY capture_id.
The orders update filters to currency_code = 'GBP' so non-GBP fees don't
silently pollute the GBP-named klarna_fee_gbp column. See
project_paypal_api_gotchas for the same pattern applied to PayPal.

orders.total_payment_fees is a GENERATED stored column — self-maintains
when klarna_fee_gbp changes.

Usage:
  python3 klarna_settlements_sync.py                  # incremental, last 7 days
  python3 klarna_settlements_sync.py --dry-run        # incremental dry-run
  python3 klarna_settlements_sync.py --backfill       # full history from 2025-01-01
  python3 klarna_settlements_sync.py --backfill --from 2025-06-01
  python3 klarna_settlements_sync.py --backfill --dry-run
"""

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv(override=True)

KLARNA_USER = os.environ["KLARNA_USERNAME"]
KLARNA_PASS = os.environ["KLARNA_PASSWORD"]
KLARNA_BASE = os.environ["KLARNA_API_URL"].rstrip("/")

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

BRAND_ID    = os.getenv("BRAND_ID", "your_brand_id")
LOG_FILE    = os.getenv("KLARNA_LOG_FILE", "logs/klarna_settlements.log")
TIMEOUT     = int(os.getenv("REQUEST_TIMEOUT", "60"))

PAGE_SIZE              = 500
BACKFILL_START_DEFAULT = "2025-01-01"

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)
logger = logging.getLogger("klarna_settlements")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s [klarna_settlements] [%(levelname)s] %(message)s"))
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(fh)
logger.addHandler(sh)

AUTH    = (KLARNA_USER, KLARNA_PASS)
HEADERS = {"Accept": "application/json"}

# ─── HTTP ─────────────────────────────────────────────────────────────────────

def fetch_paged(endpoint_path, params):
    """
    GET endpoint_path with params, then follow `pagination.next` URLs until
    exhausted. Klarna's `next` URL has all params baked in.
    Returns a list of pages (raw JSON bodies).
    """
    url = f"{KLARNA_BASE}{endpoint_path}"
    out_pages = []
    first = True
    while url:
        r = requests.get(
            url,
            auth=AUTH,
            headers=HEADERS,
            params=params if first else None,
            timeout=TIMEOUT,
        )
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", "5")))
            continue
        r.raise_for_status()
        body = r.json()
        out_pages.append(body)
        nxt = (body.get("pagination") or {}).get("next")
        url = nxt
        first = False
    return out_pages


# ─── FORMAT ───────────────────────────────────────────────────────────────────

def _pence_to_pounds(v):
    """Klarna returns minor units; convert to decimal pounds. None passes through."""
    if v is None:
        return None
    return (Decimal(int(v)) / Decimal(100)).quantize(Decimal("0.01"))


def format_payout(p):
    t = p.get("totals") or {}
    return {
        "payment_reference":          p.get("payment_reference"),
        "brand_id":                   BRAND_ID,
        "currency_code":              p.get("currency_code"),
        "merchant_id":                p.get("merchant_id"),
        "merchant_settlement_type":   p.get("merchant_settlement_type"),
        "payout_date":                p.get("payout_date"),
        "sale_amount":                _pence_to_pounds(t.get("sale_amount")),
        "fee_amount":                 _pence_to_pounds(t.get("fee_amount")),
        "return_amount":              _pence_to_pounds(t.get("return_amount")),
        "settlement_amount":          _pence_to_pounds(t.get("settlement_amount")),
        "tax_amount":                 _pence_to_pounds(t.get("tax_amount")),
        "commission_amount":          _pence_to_pounds(t.get("commission_amount")),
        "commission_reversal_amount": _pence_to_pounds(t.get("commission_reversal_amount")),
        "fee_correction_amount":      _pence_to_pounds(t.get("fee_correction_amount")),
        "holdback_amount":            _pence_to_pounds(t.get("holdback_amount")),
        "release_amount":             _pence_to_pounds(t.get("release_amount")),
        "repay_amount":               _pence_to_pounds(t.get("repay_amount")),
        "reversal_amount":            _pence_to_pounds(t.get("reversal_amount")),
        "charge_amount":              _pence_to_pounds(t.get("charge_amount")),
        "credit_amount":              _pence_to_pounds(t.get("credit_amount")),
        "fee_refund_amount":          _pence_to_pounds(t.get("fee_refund_amount")),
        "tax_refund_amount":          _pence_to_pounds(t.get("tax_refund_amount")),
        "deposit_amount":             _pence_to_pounds(t.get("deposit_amount")),
        "opening_debt_balance":       _pence_to_pounds(t.get("opening_debt_balance_amount")),
        "closing_debt_balance":       _pence_to_pounds(t.get("closing_debt_balance_amount")),
    }


def format_transaction(t):
    return {
        "capture_id":                                    t.get("capture_id"),
        "type":                                          t.get("type"),
        "detailed_type":                                 t.get("detailed_type"),
        "payment_reference":                             t.get("payment_reference"),
        "brand_id":                                      BRAND_ID,
        "merchant_id":                                   t.get("merchant_id"),
        "klarna_order_id":                               t.get("order_id"),
        "short_order_id":                                t.get("short_order_id"),
        "merchant_reference1":                           t.get("merchant_reference1"),
        "merchant_reference2":                           t.get("merchant_reference2"),
        "merchant_capture_reference":                    t.get("merchant_capture_reference"),
        "amount":                                        _pence_to_pounds(t.get("amount")),
        "currency_code":                                 t.get("currency_code"),
        "vat_amount":                                    _pence_to_pounds(t.get("vat_amount")),
        "vat_rate":                                      t.get("vat_rate"),
        "purchase_country":                              t.get("purchase_country"),
        "shipping_address_country":                      t.get("shipping_address_country"),
        "initial_payment_method_type":                   t.get("initial_payment_method_type"),
        "initial_payment_method_number_of_installments": t.get("initial_payment_method_number_of_installments"),
        "sale_date":                                     t.get("sale_date"),
        "capture_date":                                  t.get("capture_date"),
        "raw_payload":                                   json.dumps(t, ensure_ascii=False),
    }


# ─── DB ───────────────────────────────────────────────────────────────────────

PAYOUT_UPSERT = """
INSERT INTO klarna_payouts (
    payment_reference, brand_id, currency_code, merchant_id,
    merchant_settlement_type, payout_date,
    sale_amount, fee_amount, return_amount, settlement_amount, tax_amount,
    commission_amount, commission_reversal_amount, fee_correction_amount,
    holdback_amount, release_amount, repay_amount, reversal_amount,
    charge_amount, credit_amount, fee_refund_amount, tax_refund_amount,
    deposit_amount, opening_debt_balance, closing_debt_balance, synced_at
) VALUES (
    %(payment_reference)s, %(brand_id)s, %(currency_code)s, %(merchant_id)s,
    %(merchant_settlement_type)s, %(payout_date)s,
    %(sale_amount)s, %(fee_amount)s, %(return_amount)s, %(settlement_amount)s, %(tax_amount)s,
    %(commission_amount)s, %(commission_reversal_amount)s, %(fee_correction_amount)s,
    %(holdback_amount)s, %(release_amount)s, %(repay_amount)s, %(reversal_amount)s,
    %(charge_amount)s, %(credit_amount)s, %(fee_refund_amount)s, %(tax_refund_amount)s,
    %(deposit_amount)s, %(opening_debt_balance)s, %(closing_debt_balance)s, NOW()
) ON CONFLICT (payment_reference) DO UPDATE SET
    currency_code              = EXCLUDED.currency_code,
    merchant_id                = EXCLUDED.merchant_id,
    merchant_settlement_type   = EXCLUDED.merchant_settlement_type,
    payout_date                = EXCLUDED.payout_date,
    sale_amount                = EXCLUDED.sale_amount,
    fee_amount                 = EXCLUDED.fee_amount,
    return_amount              = EXCLUDED.return_amount,
    settlement_amount          = EXCLUDED.settlement_amount,
    tax_amount                 = EXCLUDED.tax_amount,
    commission_amount          = EXCLUDED.commission_amount,
    commission_reversal_amount = EXCLUDED.commission_reversal_amount,
    fee_correction_amount      = EXCLUDED.fee_correction_amount,
    holdback_amount            = EXCLUDED.holdback_amount,
    release_amount             = EXCLUDED.release_amount,
    repay_amount               = EXCLUDED.repay_amount,
    reversal_amount            = EXCLUDED.reversal_amount,
    charge_amount              = EXCLUDED.charge_amount,
    credit_amount              = EXCLUDED.credit_amount,
    fee_refund_amount          = EXCLUDED.fee_refund_amount,
    tax_refund_amount          = EXCLUDED.tax_refund_amount,
    deposit_amount             = EXCLUDED.deposit_amount,
    opening_debt_balance       = EXCLUDED.opening_debt_balance,
    closing_debt_balance       = EXCLUDED.closing_debt_balance,
    synced_at                  = NOW();
"""

TXN_UPSERT = """
INSERT INTO klarna_transactions (
    capture_id, type, detailed_type, payment_reference, brand_id, merchant_id,
    klarna_order_id, short_order_id,
    merchant_reference1, merchant_reference2, merchant_capture_reference,
    amount, currency_code, vat_amount, vat_rate,
    purchase_country, shipping_address_country,
    initial_payment_method_type, initial_payment_method_number_of_installments,
    sale_date, capture_date, raw_payload, synced_at
) VALUES (
    %(capture_id)s, %(type)s, %(detailed_type)s, %(payment_reference)s, %(brand_id)s, %(merchant_id)s,
    %(klarna_order_id)s, %(short_order_id)s,
    %(merchant_reference1)s, %(merchant_reference2)s, %(merchant_capture_reference)s,
    %(amount)s, %(currency_code)s, %(vat_amount)s, %(vat_rate)s,
    %(purchase_country)s, %(shipping_address_country)s,
    %(initial_payment_method_type)s, %(initial_payment_method_number_of_installments)s,
    %(sale_date)s, %(capture_date)s, %(raw_payload)s::jsonb, NOW()
) ON CONFLICT (COALESCE(capture_id, klarna_order_id), type, detailed_type) DO UPDATE SET
    payment_reference                             = EXCLUDED.payment_reference,
    merchant_id                                   = EXCLUDED.merchant_id,
    klarna_order_id                               = EXCLUDED.klarna_order_id,
    short_order_id                                = EXCLUDED.short_order_id,
    merchant_reference1                           = EXCLUDED.merchant_reference1,
    merchant_reference2                           = EXCLUDED.merchant_reference2,
    merchant_capture_reference                    = EXCLUDED.merchant_capture_reference,
    amount                                        = EXCLUDED.amount,
    currency_code                                 = EXCLUDED.currency_code,
    vat_amount                                    = EXCLUDED.vat_amount,
    vat_rate                                      = EXCLUDED.vat_rate,
    purchase_country                              = EXCLUDED.purchase_country,
    shipping_address_country                      = EXCLUDED.shipping_address_country,
    initial_payment_method_type                   = EXCLUDED.initial_payment_method_type,
    initial_payment_method_number_of_installments = EXCLUDED.initial_payment_method_number_of_installments,
    sale_date                                     = EXCLUDED.sale_date,
    capture_date                                  = EXCLUDED.capture_date,
    raw_payload                                   = EXCLUDED.raw_payload,
    synced_at                                     = NOW();
"""

# Aggregate SALE + FEE rows per Shopify order, then derive the GBP fee via
# the order-level FX ratio. Same approach as paypal_transactions_sync —
# (fee/sale) is the fee rate in Klarna's transaction currency; multiplying
# by orders.revenue_gbp applies the FX rate Shopify already used at sale
# time. No FX table, no API call, idempotent.
#
# History:
#   v1 — no filter; wrote raw foreign-currency fee values into the GBP
#        column (silent contamination on the small SEK fraction).
#   v2 — fee_currency = 'GBP' filter mirrored the PayPal v2 fix. Worked
#        but undercounted: 137/181 Klarna orders stamped, £333.88 total.
#   v3 (current) — order-level ratio. Stamps every Klarna order whose
#        SALE row is bridged. Same numerics as PayPal v3 — see
#        project_klarna_api_gotchas.
#
# A single Klarna order produces THREE rows (SALE / PURCHASE_FEE_PERCENTAGE
# / PURCHASE_FEE_FIXED) all sharing capture_id. We sum the two FEE rows
# and divide by the SALE row's amount to get the fee rate per Klarna order.
ORDERS_FEE_UPDATE = """
UPDATE orders o
SET klarna_fee_gbp = ROUND((cap.total_fee / cap.sale_amount * o.revenue_gbp)::numeric, 4)
FROM (
    SELECT
        sot.order_id,
        SUM(CASE WHEN kt.type = 'FEE'  THEN kt.amount END) AS total_fee,
        SUM(CASE WHEN kt.type = 'SALE' THEN kt.amount END) AS sale_amount
    FROM klarna_transactions kt
    JOIN shopify_order_transactions sot
        ON sot.receipt ->> 'payment_id' = kt.merchant_reference2
       AND sot.gateway   = 'Klarna'
       AND sot.kind      IN ('sale','capture')
       AND sot.brand_id  = %s
    WHERE kt.type IN ('SALE','FEE')
      AND kt.brand_id = %s
    GROUP BY sot.order_id
    HAVING SUM(CASE WHEN kt.type = 'SALE' THEN kt.amount END) > 0
) cap
WHERE o.order_id = cap.order_id
  AND o.brand_id = %s
  AND o.revenue_gbp IS NOT NULL;
"""


def db_connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)


def upsert_payouts(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, PAYOUT_UPSERT, rows, page_size=200)


def upsert_transactions(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, TXN_UPSERT, rows, page_size=500)


def update_orders_fees(conn):
    with conn.cursor() as cur:
        cur.execute(ORDERS_FEE_UPDATE, (BRAND_ID, BRAND_ID, BRAND_ID))
        return cur.rowcount


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="Full history from --from (default 2025-01-01).")
    parser.add_argument("--from", dest="from_date", help="Backfill cutoff YYYY-MM-DD (with --backfill).")
    parser.add_argument("--dry-run", action="store_true", help="Fetch + summarise, no DB writes.")
    args = parser.parse_args()

    mode = "BACKFILL" if args.backfill else "INCREMENTAL"
    dry  = " (DRY RUN)" if args.dry_run else ""
    logger.info(f"klarna_settlements_sync starting — {mode}{dry}")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    if args.backfill:
        start_date = (args.from_date or BACKFILL_START_DEFAULT)
        start_dt   = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt     = now_utc
    else:
        start_dt   = now_utc - timedelta(days=7)
        end_dt     = now_utc

    params = {
        "start_date": start_dt.date().isoformat(),
        "end_date":   end_dt.date().isoformat(),
        "size":       PAGE_SIZE,
    }
    logger.info(f"  window: {params['start_date']} → {params['end_date']}  size={PAGE_SIZE}")

    # Fetch payouts
    t0 = time.monotonic()
    logger.info("Fetching payouts...")
    raw_payouts = []
    for page in fetch_paged("/settlements/v1/payouts", params):
        raw_payouts.extend(page.get("payouts", []))
    logger.info(f"  payouts fetched: {len(raw_payouts)}")

    # Fetch transactions
    logger.info("Fetching transactions...")
    raw_txns = []
    for page in fetch_paged("/settlements/v1/transactions", params):
        raw_txns.extend(page.get("transactions", []))
    logger.info(f"  transactions fetched: {len(raw_txns)}")

    api_seconds = time.monotonic() - t0
    logger.info(f"  API time: {api_seconds:.1f}s")

    # Format
    payouts = [format_payout(p) for p in raw_payouts]
    txns    = [format_transaction(t) for t in raw_txns]

    # Summary
    type_ct        = Counter(t["type"]          for t in txns)
    detailed_ct    = Counter(t["detailed_type"] for t in txns)
    cur_ct         = Counter(t["currency_code"] for t in txns)
    pmt_ct         = Counter(t["initial_payment_method_type"] for t in txns)
    distinct_caps  = len({t["capture_id"] for t in txns if t["capture_id"]})
    payout_currs   = Counter(p["currency_code"] for p in payouts)

    sale_rows      = [t for t in txns if t["type"] == "SALE"]
    fee_rows       = [t for t in txns if t["type"] == "FEE"]
    fee_rows_gbp   = [t for t in fee_rows if t["currency_code"] == "GBP"]
    sales_gbp      = sum((t["amount"] or Decimal(0)) for t in sale_rows if t["currency_code"] == "GBP")
    fees_gbp       = sum((t["amount"] or Decimal(0)) for t in fee_rows_gbp)
    eff_fee_pct    = (fees_gbp / sales_gbp * 100) if sales_gbp else Decimal(0)

    # Bridge-table coverage projection
    conn = db_connect()
    try:
        sale_refs = [t["merchant_reference2"] for t in sale_rows if t["merchant_reference2"]]
        bridge_match_orders = 0
        bridge_orders_total = 0
        if sale_refs:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT order_id)
                    FROM shopify_order_transactions
                    WHERE gateway = 'Klarna'
                      AND kind IN ('sale','capture')
                      AND brand_id = %s
                      AND (receipt ->> 'payment_id') = ANY(%s)
                    """,
                    (BRAND_ID, sale_refs),
                )
                bridge_match_orders = cur.fetchone()[0]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(DISTINCT order_id) FROM shopify_order_transactions WHERE brand_id = %s",
                    (BRAND_ID,),
                )
                bridge_orders_total = cur.fetchone()[0]

        bar = "=" * 60
        logger.info("")
        logger.info(bar)
        logger.info(f"KLARNA INGESTION SUMMARY — {mode}{dry}")
        logger.info(bar)
        logger.info(f"Payouts:           {len(payouts)}")
        logger.info(f"  by currency:     {dict(payout_currs)}")
        logger.info("")
        logger.info(f"Transactions:      {len(txns)}")
        logger.info(f"  distinct captures: {distinct_caps}")
        logger.info(f"  by type:         {dict(type_ct)}")
        logger.info(f"  by detailed:     {dict(detailed_ct)}")
        logger.info(f"  by currency:     {dict(cur_ct)}")
        logger.info(f"  by payment method: {dict(pmt_ct)}")
        logger.info("")
        logger.info(f"SALE rows (all curr): {len(sale_rows)}")
        logger.info(f"FEE rows (all curr):  {len(fee_rows)}")
        logger.info(f"  GBP-only sales total: £{sales_gbp:>10,.2f}")
        logger.info(f"  GBP-only fees total:  £{fees_gbp:>10,.2f}")
        logger.info(f"  GBP effective fee rate: {eff_fee_pct:.3f}%")
        logger.info("")
        logger.info(f"Bridge (shopify_order_transactions) coverage right now:")
        logger.info(f"  distinct orders in bridge:           {bridge_orders_total}")
        logger.info(f"  Klarna SALE rows matching bridge:    {bridge_match_orders}/{len(sale_rows)} orders")
        logger.info(f"  -> orders update will set klarna_fee_gbp on ~{bridge_match_orders} orders this run")
        logger.info(f"     (re-runs self-heal as the bridge backfill completes)")
        logger.info(bar)

        if sale_rows:
            sample = sale_rows[0]
            logger.info(f"Sample SALE row:")
            logger.info(f"  capture_id={sample['capture_id']}  short_order_id={sample['short_order_id']}")
            logger.info(f"  amount={sample['amount']} {sample['currency_code']}  payment_method={sample['initial_payment_method_type']}")
            logger.info(f"  merchant_reference2={sample['merchant_reference2']}")
            logger.info(f"  payment_reference={sample['payment_reference']}  sale_date={sample['sale_date']}")

        if args.dry_run:
            logger.info("")
            logger.info("Dry run complete. No DB writes. Re-run without --dry-run to apply.")
            return

        # Write phase
        logger.info("Upserting klarna_payouts...")
        upsert_payouts(conn, payouts)

        logger.info("Upserting klarna_transactions...")
        upsert_transactions(conn, txns)

        logger.info("Updating orders.klarna_fee_gbp (GBP-only)...")
        updated = update_orders_fees(conn)
        logger.info(f"  orders updated: {updated}")
        logger.info("  (orders.total_payment_fees is a GENERATED column — auto-updates)")

        conn.commit()
        logger.info("Committed.")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info("Done.")


if __name__ == "__main__":
    main()
