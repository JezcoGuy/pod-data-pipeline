"""
shopify_order_transactions_sync.py
==================================
Fetch per-order Shopify transactions and store them in shopify_order_transactions.

Why: Shopify exposes per-transaction payment data (kind/gateway/status/amount
+ a `receipt` JSONB blob) only via /orders/{id}/transactions.json, NOT via
the main /orders.json endpoint. The `authorization` field on a PayPal
transaction holds PayPal's `transaction_id` verbatim — that's the join key
to the future `paypal_transactions` table. See
project_shopify_api_gotchas memory for related notes.

Modes:
  default      — incremental: orders created in the last 7 days (UPSERT
                 catches status changes for refunds/voids)
  --backfill   — every brand order not yet in shopify_order_transactions
  --since DATE — restrict to orders created on/after YYYY-MM-DD
  --limit N    — cap number of orders processed (testing / partial backfill)
  --dry-run    — fetch + summarise but no DB writes

Usage:
  python3 shopify_order_transactions_sync.py                   # daily incremental
  python3 shopify_order_transactions_sync.py --backfill --limit 50
  python3 shopify_order_transactions_sync.py --backfill        # full historical
  python3 shopify_order_transactions_sync.py --since 2026-05-01 --dry-run
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

load_dotenv()

SHOPIFY_STORE   = os.getenv("SHOPIFY_STORE_NAME")
SHOPIFY_TOKEN   = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_VERSION     = os.getenv("SHOPIFY_API_VERSION", "2025-04")

DB_HOST         = os.getenv("DB_HOST", "localhost")
DB_PORT         = os.getenv("DB_PORT", "5432")
DB_NAME         = os.getenv("DB_NAME")
DB_USER         = os.getenv("DB_USER")
DB_PASSWORD     = os.getenv("DB_PASSWORD")

BRAND_ID        = os.getenv("BRAND_ID", "your_brand_id")
LOG_FILE        = os.getenv("OT_LOG_FILE", "logs/shopify_order_transactions.log")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
    sys.exit("ERROR: SHOPIFY_STORE_NAME and SHOPIFY_ACCESS_TOKEN must be set in .env")

BASE_URL = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{API_VERSION}"
HEADERS  = {"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"}

# Shopify REST 2 req/s leaky bucket — see project_shopify_api_gotchas
REST_MIN_INTERVAL = 0.55
REST_MAX_RETRIES  = 5
_last_call_at     = 0.0

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)
logger = logging.getLogger("shopify_order_transactions")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s [shopify_order_transactions] [%(levelname)s] %(message)s"))
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(fh)
logger.addHandler(sh)

# ─── HTTP ─────────────────────────────────────────────────────────────────────

def _throttle():
    global _last_call_at
    delta = time.monotonic() - _last_call_at
    if delta < REST_MIN_INTERVAL:
        time.sleep(REST_MIN_INTERVAL - delta)
    _last_call_at = time.monotonic()


def rest_get(url, params=None):
    for _ in range(REST_MAX_RETRIES):
        _throttle()
        r = requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", "2"))
            logger.warning(f"429 throttled, sleeping {retry_after}s")
            time.sleep(retry_after)
            continue
        if r.status_code == 404:
            return None  # order deleted from Shopify; caller treats as no txns
        r.raise_for_status()
        return r
    raise RuntimeError(f"Exhausted retries on {url}")


def fetch_transactions(order_id):
    r = rest_get(f"{BASE_URL}/orders/{order_id}/transactions.json")
    if r is None:
        return []
    return r.json().get("transactions", [])


# ─── FORMAT / UPSERT ──────────────────────────────────────────────────────────

def _d(v):
    if v in (None, ""):
        return None
    return Decimal(str(v))


def format_transaction(t, order_id):
    return {
        "transaction_id":      int(t["id"]),
        "order_id":            str(order_id),
        "brand_id":            BRAND_ID,
        "kind":                t.get("kind"),
        "gateway":             t.get("gateway"),
        "status":              t.get("status"),
        "amount":              _d(t.get("amount")),
        "currency":            t.get("currency"),
        "authorization_code":  t.get("authorization"),
        "processed_at":        t.get("processed_at") or t.get("created_at"),
        "parent_id":           int(t["parent_id"]) if t.get("parent_id") else None,
        "source_name":         t.get("source_name"),
        "receipt":             json.dumps(t.get("receipt") or {}),
    }


UPSERT = """
INSERT INTO shopify_order_transactions (
    transaction_id, order_id, brand_id, kind, gateway, status,
    amount, currency, authorization_code, processed_at, parent_id,
    source_name, receipt, synced_at
) VALUES (
    %(transaction_id)s, %(order_id)s, %(brand_id)s, %(kind)s, %(gateway)s, %(status)s,
    %(amount)s, %(currency)s, %(authorization_code)s, %(processed_at)s, %(parent_id)s,
    %(source_name)s, %(receipt)s::jsonb, NOW()
) ON CONFLICT (transaction_id) DO UPDATE SET
    kind               = EXCLUDED.kind,
    gateway            = EXCLUDED.gateway,
    status             = EXCLUDED.status,
    amount             = EXCLUDED.amount,
    currency           = EXCLUDED.currency,
    authorization_code = EXCLUDED.authorization_code,
    processed_at       = EXCLUDED.processed_at,
    parent_id          = EXCLUDED.parent_id,
    source_name        = EXCLUDED.source_name,
    receipt            = EXCLUDED.receipt,
    synced_at          = NOW();
"""


def db_connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)


def upsert_transactions(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, UPSERT, rows, page_size=200)


def select_orders_to_sync(conn, args):
    """
    Default (incremental): orders created in the last 7 days — UPSERT picks
    up status changes (e.g. partial refund on an old order won't be caught
    unless it's also in the window, but those are rare and would surface in
    a periodic --backfill).

    --backfill: orders WITHOUT any row yet in shopify_order_transactions.
    --since:    raises the date floor.
    --limit:    caps the result.
    """
    where  = ["brand_id = %s"]
    params = [BRAND_ID]

    if args.backfill:
        where.append(
            "order_id NOT IN (SELECT DISTINCT order_id FROM shopify_order_transactions)"
        )
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        where.append("created_at >= %s")
        params.append(cutoff)

    if args.since:
        where.append("created_at >= %s")
        params.append(datetime.strptime(args.since, "%Y-%m-%d"))

    sql = (
        "SELECT order_id FROM orders "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC"
    )
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [r[0] for r in cur.fetchall()]


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="Load every order missing from shopify_order_transactions.")
    parser.add_argument("--since",    help="Restrict to orders created on/after YYYY-MM-DD.")
    parser.add_argument("--limit",    type=int, help="Cap number of orders to process.")
    parser.add_argument("--dry-run",  action="store_true", help="Fetch + summarise, no DB writes.")
    args = parser.parse_args()

    mode = "BACKFILL" if args.backfill else "INCREMENTAL"
    dry = " (DRY RUN)" if args.dry_run else ""
    logger.info(f"shopify_order_transactions_sync starting — {mode}{dry}")
    if args.since:
        logger.info(f"  since: {args.since}")
    if args.limit:
        logger.info(f"  limit: {args.limit}")

    conn = db_connect()
    try:
        order_ids = select_orders_to_sync(conn, args)
        logger.info(f"Orders to process: {len(order_ids)}")
        if not order_ids:
            logger.info("Nothing to do.")
            return

        all_rows  = []
        failures  = []
        t0        = time.monotonic()
        for i, oid in enumerate(order_ids, 1):
            try:
                txns = fetch_transactions(oid)
                for t in txns:
                    all_rows.append(format_transaction(t, oid))
            except Exception as e:
                failures.append((oid, str(e)))
                logger.warning(f"  order {oid} failed: {e}")
            if i % 25 == 0 or i == len(order_ids):
                logger.info(f"  fetched {i}/{len(order_ids)}  txns={len(all_rows)}  failures={len(failures)}")

        # Summary
        kind_counts    = Counter(r["kind"]    for r in all_rows)
        gateway_counts = Counter(r["gateway"] for r in all_rows)
        status_counts  = Counter(r["status"]  for r in all_rows)

        paypal_rows         = [r for r in all_rows if (r["gateway"] or "").lower() == "paypal"]
        paypal_with_auth    = sum(1 for r in paypal_rows if r["authorization_code"])
        shopify_pay_rows    = [r for r in all_rows if (r["gateway"] or "") == "shopify_payments"]
        shopify_pay_w_auth  = sum(1 for r in shopify_pay_rows if r["authorization_code"])

        bar = "=" * 60
        logger.info("")
        logger.info(bar)
        logger.info(f"ORDER TRANSACTIONS SYNC — {mode}{dry}")
        logger.info(bar)
        logger.info(f"orders processed:   {len(order_ids)}")
        logger.info(f"transactions:       {len(all_rows)}")
        logger.info(f"failures:           {len(failures)}")
        logger.info(f"API time:           {time.monotonic() - t0:.1f}s")
        logger.info(f"by kind:            {dict(kind_counts)}")
        logger.info(f"by gateway:         {dict(gateway_counts)}")
        logger.info(f"by status:          {dict(status_counts)}")
        logger.info("")
        logger.info(f"PayPal rows:                  {len(paypal_rows)}")
        logger.info(f"  with authorization_code:    {paypal_with_auth}  (this is the join key to paypal_transactions)")
        logger.info(f"Shopify Payments rows:        {len(shopify_pay_rows)}")
        logger.info(f"  with authorization_code:    {shopify_pay_w_auth}")
        logger.info(bar)

        if all_rows:
            sample = next((r for r in paypal_rows if r["authorization_code"]), all_rows[0])
            logger.info(f"Sample row:")
            logger.info(f"  order_id={sample['order_id']}  kind={sample['kind']}  gateway={sample['gateway']}")
            logger.info(f"  amount={sample['amount']} {sample['currency']}  status={sample['status']}")
            logger.info(f"  authorization_code={sample['authorization_code']}")

        if args.dry_run:
            logger.info("")
            logger.info("Dry run complete. No DB writes. Re-run without --dry-run to apply.")
            return

        logger.info("Upserting rows...")
        upsert_transactions(conn, all_rows)
        conn.commit()
        logger.info(f"Committed {len(all_rows)} rows.")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info("Done.")


if __name__ == "__main__":
    main()
