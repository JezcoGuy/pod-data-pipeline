"""
paypal_transactions_sync.py
===========================
Ingest PayPal Transactions Search results into paypal_transactions and stamp
paypal_fee_amount / paypal_settle_amount onto orders (via the bridge table
shopify_order_transactions.authorization == paypal_transactions.transaction_id).

PayPal Reporting API caveats baked in:
- 31-day max window per call -> we chunk by 30 days
- OAuth token lasts ~9h; one fresh token per run is plenty
- transaction_event_code lives at transaction_info.transaction_event_code
  (the brief called it transaction_type — column name kept, source renamed)
- fee_amount is returned as a NEGATIVE number; we store paypal_fee = abs(fee)
- seller_receivable_breakdown does NOT exist in this response shape; net is
  computed as transaction_amount + fee_amount (signed math subtracts the fee)

The orders update is gated by transaction_type='T0006' AND status='S' so
refunds (T1107) and currency-conversion halves (T0200) don't overwrite the
sale's fee. Refund rows are still INSERTED into paypal_transactions for
forensic reconciliation.

orders.total_payment_fees is a GENERATED STORED column — it self-maintains
when paypal_fee_amount changes; do not UPDATE it directly.

Usage:
  python3 paypal_transactions_sync.py                   # incremental, last 7d
  python3 paypal_transactions_sync.py --dry-run         # incremental dry-run
  python3 paypal_transactions_sync.py --backfill        # full history from 2025-01-01
  python3 paypal_transactions_sync.py --backfill --from 2025-06-01
  python3 paypal_transactions_sync.py --backfill --dry-run
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

load_dotenv("/opt/your_brand_id/.env", override=True)

CLIENT_ID    = os.environ["PAYPAL_CLIENT_ID"]
CLIENT_SEC   = os.environ["PAYPAL_SECRET"]
PAYPAL_BASE  = os.environ["PAYPAL_BASE_URL"].rstrip("/")

DB_HOST      = os.getenv("DB_HOST", "localhost")
DB_PORT      = os.getenv("DB_PORT", "5432")
DB_NAME      = os.getenv("DB_NAME")
DB_USER      = os.getenv("DB_USER")
DB_PASSWORD  = os.getenv("DB_PASSWORD")

BRAND_ID     = os.getenv("BRAND_ID", "your_brand_id")
LOG_FILE     = os.getenv("PAYPAL_LOG_FILE", "logs/paypal_transactions.log")
TIMEOUT      = int(os.getenv("REQUEST_TIMEOUT", "60"))

# Reporting-API constraints
WINDOW_DAYS  = 30
PAGE_SIZE    = 500
BACKFILL_START_DEFAULT = "2025-01-01"

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)
logger = logging.getLogger("paypal_transactions")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s [paypal_transactions] [%(levelname)s] %(message)s"))
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(fh)
logger.addHandler(sh)

# ─── PAYPAL HTTP ──────────────────────────────────────────────────────────────

def get_token():
    r = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(CLIENT_ID, CLIENT_SEC),
        data={"grant_type": "client_credentials"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    needed = "https://uri.paypal.com/services/reporting/search/read"
    if needed not in body.get("scope", ""):
        raise RuntimeError(f"Token missing scope {needed}. Granted: {body.get('scope')}")
    return body["access_token"]


def fetch_window(token, start_dt, end_dt):
    """Pull every transaction in [start_dt, end_dt] across all pages."""
    out = []
    page = 1
    while True:
        params = {
            "start_date": start_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "end_date":   end_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "fields":     "all",
            "page_size":  PAGE_SIZE,
            "page":       page,
        }
        r = requests.get(
            f"{PAYPAL_BASE}/v1/reporting/transactions",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            timeout=TIMEOUT,
        )
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", "5")))
            continue
        r.raise_for_status()
        body = r.json()
        details = body.get("transaction_details", [])
        out.extend(details)
        total_pages = body.get("total_pages", 1)
        if page >= total_pages or not details:
            return out
        page += 1


# ─── FORMAT ───────────────────────────────────────────────────────────────────

def _d(v):
    if v in (None, ""):
        return None
    return Decimal(str(v))


def _payer_name(payer_info):
    pn = (payer_info or {}).get("payer_name") or {}
    if not pn:
        return None
    if pn.get("alternate_full_name"):
        return pn["alternate_full_name"]
    parts = [pn.get("given_name"), pn.get("surname")]
    parts = [p for p in parts if p]
    return " ".join(parts) if parts else None


def format_row(d):
    """Map one PayPal `transaction_details` object to a paypal_transactions row."""
    ti = d.get("transaction_info") or {}
    pi = d.get("payer_info") or {}

    tx_amt_obj  = ti.get("transaction_amount") or {}
    fee_obj     = ti.get("fee_amount")        or {}
    tx_amount   = _d(tx_amt_obj.get("value"))
    fee_signed  = _d(fee_obj.get("value"))        # NEGATIVE when present

    paypal_fee = abs(fee_signed) if fee_signed is not None else None
    if tx_amount is not None and fee_signed is not None:
        net_amount = tx_amount + fee_signed       # signed math (subtracts fee)
    else:
        net_amount = tx_amount                    # T0200 etc. — no fee component

    return {
        "transaction_id":       ti.get("transaction_id"),
        "brand_id":             BRAND_ID,
        "paypal_reference_id":  ti.get("paypal_reference_id"),
        "paypal_reference_type": ti.get("paypal_reference_id_type"),
        "shopify_order_id":     None,             # resolved by SQL after upsert
        "order_name":           None,
        "transaction_type":     ti.get("transaction_event_code"),
        "transaction_status":   ti.get("transaction_status"),
        "transaction_amount":   tx_amount,
        "transaction_currency": tx_amt_obj.get("currency_code"),
        "paypal_fee":           paypal_fee,
        "net_amount":           net_amount,
        "fee_currency":         fee_obj.get("currency_code") or tx_amt_obj.get("currency_code"),
        "payer_email":          pi.get("email_address"),
        "payer_name":           _payer_name(pi),
        "transaction_initiated": ti.get("transaction_initiation_date"),
        "transaction_updated":   ti.get("transaction_updated_date"),
        "raw_payload":          json.dumps(d, ensure_ascii=False),
    }


# ─── DB ───────────────────────────────────────────────────────────────────────

UPSERT = """
INSERT INTO paypal_transactions (
    transaction_id, brand_id, paypal_reference_id, paypal_reference_type,
    shopify_order_id, order_name,
    transaction_type, transaction_status, transaction_amount, transaction_currency,
    paypal_fee, net_amount, fee_currency,
    payer_email, payer_name,
    transaction_initiated, transaction_updated, raw_payload, synced_at
) VALUES (
    %(transaction_id)s, %(brand_id)s, %(paypal_reference_id)s, %(paypal_reference_type)s,
    %(shopify_order_id)s, %(order_name)s,
    %(transaction_type)s, %(transaction_status)s, %(transaction_amount)s, %(transaction_currency)s,
    %(paypal_fee)s, %(net_amount)s, %(fee_currency)s,
    %(payer_email)s, %(payer_name)s,
    %(transaction_initiated)s, %(transaction_updated)s, %(raw_payload)s::jsonb, NOW()
) ON CONFLICT (transaction_id) DO UPDATE SET
    paypal_reference_id   = EXCLUDED.paypal_reference_id,
    paypal_reference_type = EXCLUDED.paypal_reference_type,
    transaction_type      = EXCLUDED.transaction_type,
    transaction_status    = EXCLUDED.transaction_status,
    transaction_amount    = EXCLUDED.transaction_amount,
    transaction_currency  = EXCLUDED.transaction_currency,
    paypal_fee            = EXCLUDED.paypal_fee,
    net_amount            = EXCLUDED.net_amount,
    fee_currency          = EXCLUDED.fee_currency,
    payer_email           = EXCLUDED.payer_email,
    payer_name            = EXCLUDED.payer_name,
    transaction_initiated = EXCLUDED.transaction_initiated,
    transaction_updated   = EXCLUDED.transaction_updated,
    raw_payload           = EXCLUDED.raw_payload,
    synced_at             = NOW();
"""

# Self-healing bridge resolution. Runs every sync, so when more rows land in
# shopify_order_transactions (via its own backfill) the corresponding paypal
# rows pick up their shopify_order_id without a separate rerun.
RESOLVE_BRIDGE = """
UPDATE paypal_transactions pt
SET shopify_order_id = sot.order_id,
    order_name       = o.order_name
FROM shopify_order_transactions sot
JOIN orders o ON o.order_id = sot.order_id
WHERE sot.authorization_code = pt.transaction_id
  AND sot.gateway       = 'paypal'
  AND sot.kind          IN ('sale','capture')
  AND sot.brand_id      = %s
  AND o.brand_id        = %s
  AND (pt.shopify_order_id IS NULL OR pt.shopify_order_id <> sot.order_id);
"""

# Stamp PayPal fees onto matched orders (sale rows only; refunds left alone).
#
# History of this clause:
#   v1 — no filter; wrote raw foreign-currency values into the GBP-named
#        paypal_fee_amount column. Distorted any revenue_gbp-based fee %
#        calc on the ~85% of orders settled in USD/EUR/etc.
#   v2 — added fee_currency = 'GBP' filter to keep the column honest.
#        Trade-off: left ~2,732 of 3,228 PayPal orders NULL because PayPal
#        always charges fees in the buyer's transaction currency.
#   v3 (current) — compute GBP equivalent via the order-level ratio:
#        paypal_fee_gbp = paypal_fee / transaction_amount * revenue_gbp
#        The (fee / amount) ratio is the fee rate in the original currency;
#        multiplying by orders.revenue_gbp converts it using Shopify's
#        already-applied FX rate. No FX table or API needed; numerically
#        identical to applying the rate that was actually used at sale time.
ORDERS_FEE_UPDATE = """
UPDATE orders o
SET paypal_fee_amount    = ROUND((pt.paypal_fee / pt.transaction_amount * o.revenue_gbp)::numeric, 4),
    paypal_settle_amount = ROUND((pt.net_amount / pt.transaction_amount * o.revenue_gbp)::numeric, 4)
FROM paypal_transactions pt
WHERE pt.shopify_order_id   = o.order_id
  AND pt.transaction_status = 'S'
  AND pt.transaction_type   = 'T0006'
  AND pt.paypal_fee         IS NOT NULL
  AND pt.transaction_amount > 0
  AND o.revenue_gbp         IS NOT NULL
  AND pt.brand_id           = %s
  AND o.brand_id            = %s;
"""


def db_connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)


def upsert_rows(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, UPSERT, rows, page_size=200)


def resolve_bridge(conn):
    with conn.cursor() as cur:
        cur.execute(RESOLVE_BRIDGE, (BRAND_ID, BRAND_ID))
        return cur.rowcount


def update_orders_fees(conn):
    with conn.cursor() as cur:
        cur.execute(ORDERS_FEE_UPDATE, (BRAND_ID, BRAND_ID))
        return cur.rowcount


# ─── WINDOWS ──────────────────────────────────────────────────────────────────

def iter_windows(start_dt, end_dt):
    """Yield (window_start, window_end) tuples, each <= WINDOW_DAYS long."""
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + timedelta(days=WINDOW_DAYS), end_dt)
        yield cur, nxt
        cur = nxt


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="Full history starting from --from (default 2025-01-01).")
    parser.add_argument("--from", dest="from_date",
                        help="Backfill cutoff YYYY-MM-DD (with --backfill).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + summarise, no DB writes.")
    args = parser.parse_args()

    mode = "BACKFILL" if args.backfill else "INCREMENTAL"
    dry  = " (DRY RUN)" if args.dry_run else ""
    logger.info(f"paypal_transactions_sync starting — {mode}{dry}")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    if args.backfill:
        start_str = args.from_date or BACKFILL_START_DEFAULT
        start_dt  = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt    = now_utc
    else:
        start_dt = now_utc - timedelta(days=7)
        end_dt   = now_utc

    logger.info(f"  window:  {start_dt.isoformat()}  →  {end_dt.isoformat()}")

    # Auth (also re-confirms the scope at run time)
    token = get_token()
    logger.info("  OAuth token acquired, reporting scope confirmed")

    # Fetch
    t0 = time.monotonic()
    raw_all = []
    windows = list(iter_windows(start_dt, end_dt))
    for i, (ws, we) in enumerate(windows, 1):
        chunk = fetch_window(token, ws, we)
        raw_all.extend(chunk)
        logger.info(f"  window {i}/{len(windows)}  {ws.date()} → {we.date()}   transactions: {len(chunk)}   running total: {len(raw_all)}")

    api_seconds = time.monotonic() - t0
    logger.info(f"  API time: {api_seconds:.1f}s, total transactions: {len(raw_all)}")

    # Format
    rows = [format_row(d) for d in raw_all]
    # Drop any without transaction_id (shouldn't happen but defensive)
    rows = [r for r in rows if r["transaction_id"]]

    # Summary stats
    type_ct      = Counter(r["transaction_type"]   for r in rows)
    status_ct    = Counter(r["transaction_status"] for r in rows)
    cur_ct       = Counter(r["transaction_currency"] for r in rows)
    sale_rows    = [r for r in rows if r["transaction_type"] == "T0006" and r["transaction_status"] == "S"]
    refund_rows  = [r for r in rows if r["transaction_type"] == "T1107"]
    fxconv_rows  = [r for r in rows if r["transaction_type"] == "T0200"]
    gross_sales  = sum((r["transaction_amount"] or Decimal(0)) for r in sale_rows)
    total_fees   = sum((r["paypal_fee"]         or Decimal(0)) for r in sale_rows)
    sale_with_fee = sum(1 for r in sale_rows if r["paypal_fee"] is not None)

    # Bridge-table coverage projection — how many of these will resolve to a Shopify order TODAY?
    conn = db_connect()
    try:
        sale_ids = [r["transaction_id"] for r in sale_rows]
        bridge_matches = 0
        bridge_orders  = 0
        if sale_ids:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM shopify_order_transactions
                    WHERE authorization_code = ANY(%s) AND gateway = 'paypal'
                      AND kind IN ('sale','capture') AND brand_id = %s
                    """,
                    (sale_ids, BRAND_ID),
                )
                bridge_matches = cur.fetchone()[0]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(DISTINCT order_id) FROM shopify_order_transactions WHERE brand_id = %s",
                    (BRAND_ID,),
                )
                bridge_orders = cur.fetchone()[0]

        bar = "=" * 60
        logger.info("")
        logger.info(bar)
        logger.info(f"PAYPAL INGESTION SUMMARY — {mode}{dry}")
        logger.info(bar)
        logger.info(f"Date range:           {start_dt.date()} → {end_dt.date()}  ({len(windows)} window(s))")
        logger.info(f"Transactions:         {len(rows)}")
        logger.info(f"  by type:            {dict(type_ct)}")
        logger.info(f"  by status:          {dict(status_ct)}")
        logger.info(f"  by currency:        {dict(cur_ct)}")
        logger.info("")
        logger.info(f"Sales (T0006, S):     {len(sale_rows)}")
        logger.info(f"  with fee populated: {sale_with_fee}")
        logger.info(f"  gross amount:       {gross_sales} (mixed currencies)")
        logger.info(f"  total fees:         {total_fees} (mixed currencies)")
        logger.info(f"Refunds (T1107):      {len(refund_rows)}")
        logger.info(f"FX conversions(T0200): {len(fxconv_rows)}")
        logger.info("")
        logger.info(f"Bridge-table (shopify_order_transactions) coverage right now:")
        logger.info(f"  distinct orders in bridge:    {bridge_orders}  (will grow with bridge backfill)")
        logger.info(f"  paypal sales matching bridge: {bridge_matches} / {len(sale_rows)}")
        logger.info(f"  -> orders update will set fees on ~{bridge_matches} orders this run")
        logger.info(f"     (re-runs will pick up more once bridge is back-filled — self-healing)")
        logger.info(bar)

        if rows:
            sample = next((r for r in sale_rows if r["paypal_fee"]), rows[0])
            logger.info(f"Sample sale row:")
            logger.info(f"  transaction_id={sample['transaction_id']}  type={sample['transaction_type']}  status={sample['transaction_status']}")
            logger.info(f"  amount={sample['transaction_amount']} {sample['transaction_currency']}")
            logger.info(f"  fee={sample['paypal_fee']} {sample['fee_currency']}  net={sample['net_amount']}")
            logger.info(f"  payer={sample['payer_email']} ({sample['payer_name']})  initiated={sample['transaction_initiated']}")

        if args.dry_run:
            logger.info("")
            logger.info("Dry run complete. No DB writes. Re-run without --dry-run to apply.")
            return

        # Write phase
        logger.info("Upserting paypal_transactions...")
        upsert_rows(conn, rows)

        logger.info("Resolving bridge to shopify_order_id / order_name...")
        bridged = resolve_bridge(conn)
        logger.info(f"  rows resolved (or refreshed) this pass: {bridged}")

        logger.info("Updating orders.paypal_fee_amount + paypal_settle_amount (T0006, S only)...")
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
