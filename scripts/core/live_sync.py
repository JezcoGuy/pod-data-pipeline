"""
live_sync.py
============
Hourly snapshot of today's Shopify orders + Meta ad spend → live_snapshot.

Two tiny API calls per run, no dependency on the nightly syncs:
  1. Shopify /orders.json filtered to local-today → orders count, items, revenue
  2. Meta /insights with date_preset=today          → spend, impressions, clicks

Gross figures only (no refund handling) — by design, for at-a-glance MER.
Refund-adjusted views live in the nightly P&L.

Run hourly via cron. Each run upserts one row keyed on
(brand_id, snapshot_date, snapshot_hour), so multiple runs in the same hour
just refresh the same row.

Usage:
    python3 live_sync.py
    python3 live_sync.py --dry-run
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
import psycopg2
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv()

SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
SHOPIFY_STORE        = os.getenv("SHOPIFY_STORE_NAME")

META_ACCESS_TOKEN    = os.getenv("META_ACCESS_TOKEN")
META_AD_ACCOUNT_ID   = (os.getenv("META_AD_ACCOUNT_ID", "")
                        .lstrip("act_").strip())
META_API_VERSION     = os.getenv("META_API_VERSION", "v21.0")

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

BRAND_ID         = os.getenv("BRAND_ID", "your_brand_id")
REQUEST_TIMEOUT  = int(os.getenv("REQUEST_TIMEOUT", "30"))
LOCAL_TZ         = ZoneInfo(os.getenv("LOCAL_TZ", "Europe/London"))
LOG_FILE         = os.getenv("LIVE_LOG_FILE", "logs/live_sync.log")

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)

logger = logging.getLogger("live_sync")
logger.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s [%(name)s] [%(levelname)s] %(message)s")

fh = logging.FileHandler(LOG_FILE)
fh.setLevel(logging.INFO)
fh.setFormatter(fmt)
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(fmt)
logger.addHandler(ch)

# ─── DB ───────────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )
    conn.autocommit = False
    return conn

# ─── SHOPIFY ──────────────────────────────────────────────────────────────────

SHOPIFY_BASE = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2025-04"
SHOPIFY_HEADERS = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}


def shopify_get(url, params=None, max_retries=3):
    backoff = 1
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers=SHOPIFY_HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", backoff))
                logger.warning(f"Shopify 429 — retry in {wait}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 30)
                continue
            if resp.status_code >= 500:
                logger.warning(f"Shopify {resp.status_code} — retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Shopify connection error: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    raise Exception(f"Shopify request failed: {url}")


def fetch_shopify_today(today_start_utc_iso):
    """Sum today's orders: count, total items, gross revenue. Paginates via Link header."""
    url = f"{SHOPIFY_BASE}/orders.json"
    params = {
        "status":         "any",
        "limit":          250,
        "created_at_min": today_start_utc_iso,
    }
    orders_count = 0
    items_total  = 0
    revenue_gbp  = 0.0

    while url:
        resp = shopify_get(url, params=params)
        batch = resp.json().get("orders", []) or []
        for o in batch:
            orders_count += 1
            for li in (o.get("line_items") or []):
                items_total += int(li.get("quantity") or 0)
            try:
                revenue_gbp += float(o.get("total_price") or 0)
            except (TypeError, ValueError):
                pass
        link = resp.headers.get("Link", "")
        if 'rel="next"' not in link:
            break
        parts = [p.split(";") for p in link.split(",")]
        url = next(
            (p[0].strip("<> ") for p in parts if len(p) > 1 and 'rel="next"' in p[1]),
            None,
        )
        params = None  # next URL embeds params

    return orders_count, items_total, round(revenue_gbp, 4)

# ─── META ─────────────────────────────────────────────────────────────────────

META_BASE = f"https://graph.facebook.com/{META_API_VERSION}"


def fetch_meta_today():
    """One /insights call for today, level=account so it returns one summed row."""
    url = f"{META_BASE}/act_{META_AD_ACCOUNT_ID}/insights"
    params = {
        "fields":        "spend,impressions,clicks",
        "level":         "account",
        "date_preset":   "today",
        "access_token":  META_ACCESS_TOKEN,
    }
    backoff = 2
    for attempt in range(4):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429 or (resp.status_code == 403 and "rate" in resp.text.lower()):
                logger.warning(f"Meta rate-limit — retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 500:
                logger.warning(f"Meta {resp.status_code} — retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            logger.warning(f"Meta error: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    else:
        raise Exception("Meta /insights failed after retries")

    data = (resp.json().get("data") or [])
    if not data:
        # No spend today (yet) — return zeros
        return 0.0, 0, 0

    row = data[0]
    spend       = float(row.get("spend") or 0)
    impressions = int(row.get("impressions") or 0)
    clicks      = int(row.get("clicks") or 0)
    return round(spend, 4), impressions, clicks

# ─── UPSERT ───────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO live_snapshot (
    snapshot_at, snapshot_date, snapshot_hour, brand_id,
    shopify_orders_today, shopify_items_today, shopify_revenue_today_gbp,
    meta_spend_today_gbp, meta_impressions_today, meta_clicks_today,
    synced_at
) VALUES (
    %(snapshot_at)s, %(snapshot_date)s, %(snapshot_hour)s, %(brand_id)s,
    %(shopify_orders_today)s, %(shopify_items_today)s, %(shopify_revenue_today_gbp)s,
    %(meta_spend_today_gbp)s, %(meta_impressions_today)s, %(meta_clicks_today)s,
    %(synced_at)s
)
ON CONFLICT (brand_id, snapshot_date, snapshot_hour) DO UPDATE SET
    snapshot_at               = EXCLUDED.snapshot_at,
    shopify_orders_today      = EXCLUDED.shopify_orders_today,
    shopify_items_today       = EXCLUDED.shopify_items_today,
    shopify_revenue_today_gbp = EXCLUDED.shopify_revenue_today_gbp,
    meta_spend_today_gbp      = EXCLUDED.meta_spend_today_gbp,
    meta_impressions_today    = EXCLUDED.meta_impressions_today,
    meta_clicks_today         = EXCLUDED.meta_clicks_today,
    synced_at                 = EXCLUDED.synced_at
"""

# ─── RUN ──────────────────────────────────────────────────────────────────────

def run_sync(dry_run=False):
    now_utc    = datetime.now(timezone.utc)
    now_local  = now_utc.astimezone(LOCAL_TZ)
    today_local = now_local.date()
    today_start_local = datetime.combine(today_local, datetime.min.time(), tzinfo=LOCAL_TZ)
    today_start_utc   = today_start_local.astimezone(timezone.utc)
    today_start_iso   = today_start_utc.isoformat()

    logger.info(
        f"Live sync — local now {now_local.isoformat()} "
        f"(snapshot_date={today_local} hour={now_local.hour})"
    )

    orders_count, items_total, revenue_gbp = fetch_shopify_today(today_start_iso)
    logger.info(
        f"Shopify today: orders={orders_count} items={items_total} revenue=£{revenue_gbp}"
    )

    spend, impressions, clicks = fetch_meta_today()
    logger.info(
        f"Meta today: spend=£{spend} impressions={impressions} clicks={clicks}"
    )

    mer = (revenue_gbp / spend) if spend > 0 else None
    logger.info(f"MER (gross): {mer:.2f}" if mer else "MER (gross): n/a (no spend yet)")

    if dry_run:
        logger.info("[DRY RUN] No DB write")
        return

    row = {
        "snapshot_at":               now_utc.isoformat(),
        "snapshot_date":             today_local,
        "snapshot_hour":             now_local.hour,
        "brand_id":                  BRAND_ID,
        "shopify_orders_today":      orders_count,
        "shopify_items_today":       items_total,
        "shopify_revenue_today_gbp": revenue_gbp,
        "meta_spend_today_gbp":      spend,
        "meta_impressions_today":    impressions,
        "meta_clicks_today":         clicks,
        "synced_at":                 now_utc.isoformat(),
    }

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(UPSERT_SQL, row)
        conn.commit()
        logger.info(f"Upserted live_snapshot row for {today_local} hour {now_local.hour}")
    finally:
        conn.close()

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live MER snapshot (Shopify + Meta)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch + log, no DB write")
    args = parser.parse_args()

    try:
        run_sync(dry_run=args.dry_run)
        sys.exit(0)
    except Exception as e:
        logger.error(f"live_sync failed: {e}")
        sys.exit(1)
