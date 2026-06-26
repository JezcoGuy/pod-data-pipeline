"""
shopify_payouts_sync.py
=======================
Ingest Shopify Payments payouts + per-transaction lines into Postgres
and stamp shopify_fee_gbp / shopify_fee_pct onto the orders table.

Default mode is a 7-day incremental sync. Use --backfill (optionally with
--from YYYY-MM-DD) to load history. --dry-run reports what would happen
without writing.

API endpoints (REST, 2026-01):
  GET /shopify_payments/payouts.json
  GET /shopify_payments/balance/transactions.json?payout_id={id}
  GET /orders.json?fields=id,payment_gateway_names  (used by --backfill to
      back-fill orders.payment_gateway from payment_gateway_names)

Run via cron (see brief):
  0 6 * * * cd /opt/your_brand_id && python3 scripts/shopify_payouts_sync.py >> logs/cron.log 2>&1

Usage:
  python3 shopify_payouts_sync.py                       # incremental (7d)
  python3 shopify_payouts_sync.py --dry-run             # incremental dry-run
  python3 shopify_payouts_sync.py --backfill            # full history
  python3 shopify_payouts_sync.py --backfill --from 2025-01-01
  python3 shopify_payouts_sync.py --backfill --dry-run  # safe preview
"""

import argparse
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
API_VERSION     = os.getenv("SHOPIFY_PAYOUTS_API_VERSION", "2026-01")

DB_HOST         = os.getenv("DB_HOST", "localhost")
DB_PORT         = os.getenv("DB_PORT", "5432")
DB_NAME         = os.getenv("DB_NAME")
DB_USER         = os.getenv("DB_USER")
DB_PASSWORD     = os.getenv("DB_PASSWORD")

BRAND_ID        = os.getenv("BRAND_ID", "your_brand_id")
LOG_FILE        = os.getenv("PAYOUTS_LOG_FILE", "logs/shopify_payouts.log")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))

if not SHOPIFY_STORE or not SHOPIFY_TOKEN:
    sys.exit("ERROR: SHOPIFY_STORE_NAME and SHOPIFY_ACCESS_TOKEN must be set in .env")

BASE_URL = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/{API_VERSION}"
HEADERS  = {"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"}

# Shopify REST = 2 req/s leaky bucket. See project_shopify_api_gotchas memory.
REST_MIN_INTERVAL = 0.55
REST_MAX_RETRIES  = 5
_last_call_at     = 0.0

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)
logger = logging.getLogger("shopify_payouts")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s [shopify_payouts] [%(levelname)s] %(message)s"))
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
    """GET with 0.55s pacing + 429 retry honouring Retry-After."""
    for _ in range(REST_MAX_RETRIES):
        _throttle()
        r = requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 429:
            retry_after = float(r.headers.get("Retry-After", "2"))
            logger.warning(f"429 throttled, sleeping {retry_after}s")
            time.sleep(retry_after)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"Exhausted retries on {url}")


def _next_link(link_header):
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            s = part.find("<") + 1
            e = part.find(">")
            if s > 0 and e > s:
                return part[s:e]
    return None


def paginate(path, params=None):
    """Yield each JSON page across Link-header pagination."""
    url = f"{BASE_URL}{path}"
    first = True
    while True:
        r = rest_get(url, params=params if first else None)
        yield r.json()
        nxt = _next_link(r.headers.get("Link"))
        if not nxt:
            return
        url = nxt
        first = False


# ─── FETCHERS ─────────────────────────────────────────────────────────────────

def fetch_payouts(date_min=None):
    params = {"limit": 250}
    if date_min:
        params["date_min"] = date_min.strftime("%Y-%m-%d")
    out = []
    for page in paginate("/shopify_payments/payouts.json", params=params):
        out.extend(page.get("payouts", []))
    return out


def fetch_payout_lines(payout_id):
    params = {"payout_id": payout_id, "limit": 250}
    out = []
    for page in paginate("/shopify_payments/balance/transactions.json", params=params):
        out.extend(page.get("transactions", []))
    return out


def fetch_order_id_to_name(conn, order_ids):
    """Look up {source_order_id: order_name} from the orders table."""
    if not order_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT order_id, order_name FROM orders WHERE brand_id = %s AND order_id::bigint = ANY(%s)",
            (BRAND_ID, list(order_ids)),
        )
        rows = cur.fetchall()
    return {int(r[0]): r[1] for r in rows}


def fetch_all_order_gateways():
    """Stream orders with just (id, payment_gateway_names) for the gateway back-fill."""
    params = {
        "status": "any",
        "limit": 250,
        "fields": "id,payment_gateway_names",
    }
    out = {}
    for page in paginate("/orders.json", params=params):
        for o in page.get("orders", []):
            names = o.get("payment_gateway_names") or []
            out[str(o["id"])] = ",".join(names)
    return out


# ─── FORMATTERS ───────────────────────────────────────────────────────────────

def _d(v):
    if v in (None, ""):
        return None
    return Decimal(str(v))


def format_payout(p):
    s = p.get("summary") or {}
    return {
        "payout_id":            int(p["id"]),
        "brand_id":             BRAND_ID,
        "status":               p.get("status"),
        "payout_date":          p.get("date"),
        "currency":             p.get("currency"),
        "amount_gbp":           _d(p.get("amount")),
        "charges_gross":        _d(s.get("charges_gross_amount")),
        "charges_fees":         _d(s.get("charges_fee_amount")),
        "refunds_gross":        _d(s.get("refunds_gross_amount")),
        "refunds_fees":         _d(s.get("refunds_fee_amount")),
        "adjustments_gross":    _d(s.get("adjustments_gross_amount")),
        "adjustments_fees":     _d(s.get("adjustments_fee_amount")),
        "reserved_funds_gross": _d(s.get("reserved_funds_gross_amount")),
    }


def format_line(line, order_name_map):
    source_order_id = line.get("source_order_id")
    order_name = order_name_map.get(source_order_id) if source_order_id else None
    return {
        "line_id":         int(line["id"]),
        "payout_id":       int(line["payout_id"]) if line.get("payout_id") else None,
        "brand_id":        BRAND_ID,
        "type":            line.get("type"),
        "source_type":     line.get("source_type"),
        "source_order_id": source_order_id,
        "order_name":      order_name,
        "currency":        line.get("currency"),
        "amount":          _d(line.get("amount")),
        "fee":             _d(line.get("fee")),
        "net":             _d(line.get("net")),
        "processed_at":    line.get("processed_at"),
    }


# ─── UPSERTS ──────────────────────────────────────────────────────────────────

PAYOUT_UPSERT = """
INSERT INTO shopify_payouts (
    payout_id, brand_id, status, payout_date, currency, amount_gbp,
    charges_gross, charges_fees, refunds_gross, refunds_fees,
    adjustments_gross, adjustments_fees, reserved_funds_gross, synced_at
) VALUES (
    %(payout_id)s, %(brand_id)s, %(status)s, %(payout_date)s, %(currency)s, %(amount_gbp)s,
    %(charges_gross)s, %(charges_fees)s, %(refunds_gross)s, %(refunds_fees)s,
    %(adjustments_gross)s, %(adjustments_fees)s, %(reserved_funds_gross)s, NOW()
) ON CONFLICT (payout_id) DO UPDATE SET
    status               = EXCLUDED.status,
    payout_date          = EXCLUDED.payout_date,
    currency             = EXCLUDED.currency,
    amount_gbp           = EXCLUDED.amount_gbp,
    charges_gross        = EXCLUDED.charges_gross,
    charges_fees         = EXCLUDED.charges_fees,
    refunds_gross        = EXCLUDED.refunds_gross,
    refunds_fees         = EXCLUDED.refunds_fees,
    adjustments_gross    = EXCLUDED.adjustments_gross,
    adjustments_fees     = EXCLUDED.adjustments_fees,
    reserved_funds_gross = EXCLUDED.reserved_funds_gross,
    synced_at            = NOW();
"""

LINE_UPSERT = """
INSERT INTO shopify_payout_lines (
    line_id, payout_id, brand_id, type, source_type, source_order_id, order_name,
    currency, amount, fee, net, processed_at, synced_at
) VALUES (
    %(line_id)s, %(payout_id)s, %(brand_id)s, %(type)s, %(source_type)s,
    %(source_order_id)s, %(order_name)s, %(currency)s, %(amount)s, %(fee)s, %(net)s,
    %(processed_at)s, NOW()
) ON CONFLICT (line_id) DO UPDATE SET
    payout_id       = EXCLUDED.payout_id,
    type            = EXCLUDED.type,
    source_type     = EXCLUDED.source_type,
    source_order_id = EXCLUDED.source_order_id,
    order_name      = EXCLUDED.order_name,
    currency        = EXCLUDED.currency,
    amount          = EXCLUDED.amount,
    fee             = EXCLUDED.fee,
    net             = EXCLUDED.net,
    processed_at    = EXCLUDED.processed_at,
    synced_at       = NOW();
"""

ORDERS_FEE_UPDATE = """
UPDATE orders o
SET shopify_fee_gbp = spl.fee,
    shopify_fee_pct = ROUND((spl.fee / NULLIF(o.revenue_gbp, 0) * 100)::numeric, 4)
FROM shopify_payout_lines spl
WHERE spl.source_order_id = o.order_id::bigint
  AND spl.source_type     = 'charge'
  AND spl.brand_id        = %s
  AND o.brand_id          = %s
  AND spl.fee             > 0;
"""

GATEWAY_UPDATE = """
UPDATE orders SET payment_gateway = %s
WHERE order_id = %s AND brand_id = %s
  AND COALESCE(payment_gateway, '') <> %s;
"""

# ─── DB ───────────────────────────────────────────────────────────────────────

def db_connect():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )


def upsert_payouts(conn, payouts):
    if not payouts:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, PAYOUT_UPSERT, payouts, page_size=200)


def upsert_lines(conn, lines):
    if not lines:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, LINE_UPSERT, lines, page_size=500)


def update_orders_fees(conn):
    with conn.cursor() as cur:
        cur.execute(ORDERS_FEE_UPDATE, (BRAND_ID, BRAND_ID))
        return cur.rowcount


def update_orders_gateway(conn, gateway_map):
    """Apply payment_gateway back-fill. gateway_map: {order_id_str: gateway_str}."""
    if not gateway_map:
        return 0
    rows = [(g, oid, BRAND_ID, g) for oid, g in gateway_map.items()]
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, GATEWAY_UPDATE, rows, page_size=500)
        # rowcount on the last batch only — re-query for an accurate total
        cur.execute(
            "SELECT COUNT(*) FROM orders WHERE brand_id = %s AND payment_gateway <> ''",
            (BRAND_ID,),
        )
        return cur.fetchone()[0]


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="Load full payout history (no date_min).")
    parser.add_argument("--from", dest="from_date", help="Backfill cutoff date YYYY-MM-DD (only with --backfill).")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen, no DB writes.")
    args = parser.parse_args()

    mode = "BACKFILL" if args.backfill else "INCREMENTAL"
    dry = " (DRY RUN)" if args.dry_run else ""
    logger.info(f"shopify_payouts_sync starting — {mode}{dry}")

    if args.backfill:
        if args.from_date:
            date_min = datetime.strptime(args.from_date, "%Y-%m-%d").date()
            logger.info(f"  date_min: {date_min} (from --from)")
        else:
            date_min = None
            logger.info(f"  date_min: none (full history)")
    else:
        date_min = (datetime.now(timezone.utc) - timedelta(days=7)).date()
        logger.info(f"  date_min: {date_min} (incremental 7d)")

    # Pull payouts + lines (read-only; safe under --dry-run too)
    t0 = time.monotonic()
    logger.info("Fetching payouts...")
    raw_payouts = fetch_payouts(date_min=date_min)
    logger.info(f"  payouts fetched: {len(raw_payouts)}")

    raw_lines = []
    for i, p in enumerate(raw_payouts, 1):
        lines = fetch_payout_lines(p["id"])
        raw_lines.extend(lines)
        if i % 10 == 0 or i == len(raw_payouts):
            logger.info(f"  lines fetched: payout {i}/{len(raw_payouts)} (running total {len(raw_lines)})")

    api_seconds = time.monotonic() - t0
    logger.info(f"  API time: {api_seconds:.1f}s")

    # Resolve order_name for the lines that have a source_order_id
    conn = db_connect()
    try:
        order_ids = {int(l["source_order_id"]) for l in raw_lines if l.get("source_order_id")}
        order_name_map = fetch_order_id_to_name(conn, order_ids)

        payouts = [format_payout(p) for p in raw_payouts]
        lines   = [format_line(l, order_name_map) for l in raw_lines]

        # ── Summary
        status_counts = Counter(p["status"] for p in payouts)
        type_counts   = Counter(l["type"]   for l in lines)
        src_counts    = Counter(l["source_type"] for l in lines)
        with_order_id = sum(1 for l in lines if l["source_order_id"])
        matched_orders = sum(1 for l in lines if l["order_name"])
        sum_amount   = sum((p["amount_gbp"]   or 0) for p in payouts)
        sum_charges  = sum((p["charges_gross"]or 0) for p in payouts)
        sum_fees     = sum((p["charges_fees"] or 0) for p in payouts)
        date_lo = min((p["payout_date"] for p in payouts), default=None)
        date_hi = max((p["payout_date"] for p in payouts), default=None)

        # Eligible charge lines (drive the orders fee update)
        charge_lines_with_fee = sum(1 for l in lines if l["source_type"] == "charge" and (l["fee"] or 0) > 0)
        distinct_orders_to_update = len({l["source_order_id"] for l in lines
                                         if l["source_type"] == "charge"
                                         and (l["fee"] or 0) > 0
                                         and l["source_order_id"]})

        bar = "=" * 60
        logger.info("")
        logger.info(bar)
        logger.info(f"PAYOUT INGESTION SUMMARY — {mode}{dry}")
        logger.info(bar)
        logger.info(f"Payouts:         {len(payouts)}")
        logger.info(f"  date range:    {date_lo}  →  {date_hi}")
        logger.info(f"  by status:     {dict(status_counts)}")
        logger.info(f"  net to bank:   £{sum_amount:>12,.2f}")
        logger.info(f"  charges gross: £{sum_charges:>12,.2f}")
        logger.info(f"  charges fees:  £{sum_fees:>12,.2f}")
        if sum_charges:
            eff = float(sum_fees) / float(sum_charges) * 100
            logger.info(f"  effective fee: {eff:.3f}%")
        logger.info("")
        logger.info(f"Payout lines:    {len(lines)}")
        logger.info(f"  by type:        {dict(type_counts)}")
        logger.info(f"  by source_type: {dict(src_counts)}")
        logger.info(f"  with source_order_id: {with_order_id}")
        logger.info(f"  matched in orders table: {matched_orders}")
        logger.info("")
        logger.info(f"Orders fee back-fill:")
        logger.info(f"  charge lines with fee > 0:       {charge_lines_with_fee}")
        logger.info(f"  distinct orders that would update: {distinct_orders_to_update}")
        logger.info("")

        # Most recent payout sample
        if payouts:
            sp = max(payouts, key=lambda p: p["payout_date"] or "0000-00-00")
            logger.info(f"Sample (most recent payout):")
            logger.info(f"  payout_id={sp['payout_id']} status={sp['status']} date={sp['payout_date']}")
            logger.info(f"  amount=£{sp['amount_gbp']}  charges_gross=£{sp['charges_gross']}  charges_fees=£{sp['charges_fees']}")
        if lines:
            sl = next((l for l in lines if l["source_type"] == "charge" and l["order_name"]), lines[0])
            logger.info(f"Sample line: line_id={sl['line_id']} type={sl['type']} order={sl['order_name']} amount=£{sl['amount']} fee=£{sl['fee']}")
        logger.info(bar)

        if args.dry_run:
            # Gateway back-fill estimate is API-heavy; only report it on backfill
            if args.backfill:
                logger.info("")
                logger.info("Payment-gateway back-fill (would also run in --backfill, not --dry-run):")
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM orders WHERE brand_id = %s AND COALESCE(payment_gateway,'') = ''",
                        (BRAND_ID,),
                    )
                    empty_count = cur.fetchone()[0]
                logger.info(f"  orders with payment_gateway currently empty: {empty_count}")
                logger.info(f"  would fetch payment_gateway_names from Shopify REST and update these rows")
            logger.info("")
            logger.info("Dry run complete. No DB writes. Re-run without --dry-run to apply.")
            return

        # ── Write phase ──
        logger.info("Upserting shopify_payouts...")
        upsert_payouts(conn, payouts)

        logger.info("Upserting shopify_payout_lines...")
        upsert_lines(conn, lines)

        logger.info("Updating orders fees from charge lines...")
        updated = update_orders_fees(conn)
        logger.info(f"  orders updated: {updated}")

        if args.backfill:
            logger.info("Back-filling orders.payment_gateway from Shopify orders API...")
            gateway_map = fetch_all_order_gateways()
            logger.info(f"  orders fetched: {len(gateway_map)}")
            updated = update_orders_gateway(conn, gateway_map)
            logger.info(f"  orders with non-empty payment_gateway after update: {updated}")

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
