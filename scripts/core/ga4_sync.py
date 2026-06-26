"""
ga4_sync.py
===========
Syncs Google Analytics 4 data to PostgreSQL via the GA4 Data API.
Pure data ingestion — no alert logic.

Three reports per run, all upserted by date:
  1. fetch_channels  -> ga4_channels_daily   per (date, channel, source, medium, campaign)
  2. fetch_products  -> ga4_products_daily   per (date, item_id, channel)
  3. fetch_pages     -> ga4_pages_daily      per (date, page_path)

GA4 caps Data API requests at 10 metrics per call, so the channels report is
split into two requests (volume+engagement and conversion+revenue) merged by
their shared dimension key. Products and pages fit in single requests.

Usage:
    python3 ga4_sync.py                       # last 7 days (cron default)
    python3 ga4_sync.py --lookback-days 30    # wider window for backfill
    python3 ga4_sync.py --dry-run             # fetch + summarise, no DB writes
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, timedelta, timezone
import psycopg2
from dotenv import load_dotenv

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange, Dimension, Metric, RunReportRequest, OrderBy,
)
from google.oauth2 import service_account

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv()

GA4_PROPERTY      = os.getenv("GA4_PROPERTY")
GA4_KEY_FILE      = os.getenv("GA4_KEY_FILE")

DB_HOST           = os.getenv("DB_HOST", "localhost")
DB_PORT           = os.getenv("DB_PORT", "5432")
DB_NAME           = os.getenv("DB_NAME")
DB_USER           = os.getenv("DB_USER")
DB_PASSWORD       = os.getenv("DB_PASSWORD")

BRAND_ID          = os.getenv("BRAND_ID", "your_brand_id")
DEFAULT_LOOKBACK  = int(os.getenv("GA4_LOOKBACK_DAYS", "7"))
LOG_FILE          = os.getenv("GA4_LOG_FILE", "logs/ga4_sync.log")

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)

logger = logging.getLogger("ga4_sync")
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


def test_db_connection():
    try:
        conn = get_db_connection()
        conn.close()
        logger.info("Postgres connection OK")
    except Exception as e:
        logger.critical(f"Postgres connection failed: {e}")
        sys.exit(1)

# ─── GA4 CLIENT ───────────────────────────────────────────────────────────────

def get_ga4_client():
    if not GA4_KEY_FILE or not os.path.exists(GA4_KEY_FILE):
        logger.critical(f"GA4_KEY_FILE not found or unset: {GA4_KEY_FILE!r}")
        sys.exit(1)
    if not GA4_PROPERTY:
        logger.critical("GA4_PROPERTY missing from .env")
        sys.exit(1)
    creds = service_account.Credentials.from_service_account_file(GA4_KEY_FILE)
    return BetaAnalyticsDataClient(credentials=creds)

# ─── PARSING HELPERS ──────────────────────────────────────────────────────────

def parse_ga4_date(s):
    """GA4 returns dates as 'YYYYMMDD'. Convert to date object."""
    if not s or len(s) != 8:
        return None
    return datetime.strptime(s, "%Y%m%d").date()


# GA4 item_id from Shopify-native integration: 'shopify_<store>_<product>_<variant>'.
# Extract the two numeric IDs at the end. Non-matching item_ids
# (sentinel '(not set)', null_null edge cases, manual products) return (None, None).
import re
_SHOPIFY_ITEM_ID_RE = re.compile(r"^shopify_[^_]+_(\d+)_(\d+)$")


def parse_shopify_item_id(item_id):
    if not item_id:
        return None, None
    m = _SHOPIFY_ITEM_ID_RE.match(item_id)
    if m:
        return m.group(1), m.group(2)
    return None, None


def safe_int(v, default=0):
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def safe_float(v, default=0.0):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

# ─── REPORT RUNNER ────────────────────────────────────────────────────────────

def run_report(client, dimensions, metrics, since, until, page_size=10000):
    """Run a GA4 report, paginating until exhausted. Returns list of dicts:
    {dimensions: {name: value, ...}, metrics: {name: str_value, ...}}.

    Retries on transient errors (GA4 sometimes returns 503/UNAVAILABLE).
    """
    all_rows = []
    offset = 0
    while True:
        for attempt in range(4):
            try:
                request = RunReportRequest(
                    property=f"properties/{GA4_PROPERTY}",
                    dimensions=[Dimension(name=d) for d in dimensions],
                    metrics=[Metric(name=m) for m in metrics],
                    date_ranges=[DateRange(start_date=since, end_date=until)],
                    limit=page_size,
                    offset=offset,
                )
                response = client.run_report(request)
                break
            except Exception as e:
                # 400 / INVALID_ARGUMENT: the request is malformed, retrying
                # will just fail the same way. Bail immediately.
                msg = str(e)
                if "400" in msg or "INVALID_ARGUMENT" in msg:
                    raise
                wait = 2 ** attempt
                logger.warning(f"GA4 request error (attempt {attempt+1}/4): {e}; sleeping {wait}s")
                time.sleep(wait)
        else:
            raise Exception("GA4 request failed after 4 attempts")

        dim_names = [h.name for h in response.dimension_headers]
        met_names = [h.name for h in response.metric_headers]
        for row in response.rows:
            all_rows.append({
                "dimensions": {dn: row.dimension_values[i].value for i, dn in enumerate(dim_names)},
                "metrics":    {mn: row.metric_values[i].value    for i, mn in enumerate(met_names)},
            })
        # GA4 also returns row_count (total matching). If we've fetched all, stop.
        if len(response.rows) < page_size:
            break
        offset += page_size
    return all_rows


def merge_by_dims(rows_a, rows_b, dim_names):
    """Merge two row lists by their shared dimension key, preserving all metrics.

    Useful when a single report exceeds GA4's 10-metric-per-request cap and we
    have to split it into two parallel requests with identical dimensions.
    """
    def key(row):
        return tuple(row["dimensions"][d] for d in dim_names)
    index = {key(r): r for r in rows_a}
    for r in rows_b:
        k = key(r)
        if k in index:
            index[k]["metrics"].update(r["metrics"])
        else:
            index[k] = r
    return list(index.values())

# ─── PHASE 1: CHANNELS REPORT ─────────────────────────────────────────────────
# 5 dimensions + 15 metrics — exceeds GA4's 10-metric cap, so split.

CHANNELS_DIMENSIONS = [
    "date",
    "sessionDefaultChannelGroup",
    "sessionSource",
    "sessionMedium",
    "sessionCampaignName",
]
CHANNELS_METRICS_VOLUME = [
    "sessions",
    "engagedSessions",
    "newUsers",
    "activeUsers",
    "eventCount",
    "engagementRate",
    "averageSessionDuration",
    "userEngagementDuration",
    "bounceRate",
]
CHANNELS_METRICS_CONVERSION = [
    "keyEvents",
    "addToCarts",
    "checkouts",
    "ecommercePurchases",
    "totalRevenue",
    "purchaseRevenue",
]


def fetch_channels(client, since, until):
    logger.info(f"Report 1/3: channels ({since} -> {until})")
    rows_a = run_report(client, CHANNELS_DIMENSIONS, CHANNELS_METRICS_VOLUME,     since, until)
    rows_b = run_report(client, CHANNELS_DIMENSIONS, CHANNELS_METRICS_CONVERSION, since, until)
    merged = merge_by_dims(rows_a, rows_b, CHANNELS_DIMENSIONS)
    logger.info(f"  channels merged rows: {len(merged)}")
    return merged

# ─── PHASE 2: PRODUCTS REPORT ─────────────────────────────────────────────────

PRODUCTS_DIMENSIONS = [
    "date",
    "itemId",
    "itemName",
    "sessionDefaultChannelGroup",
]
PRODUCTS_METRICS = [
    # Note: itemPurchaseQuantity and itemsPurchased map to the same internal
    # GA4 field (product_info_purchase_quantity). API rejects both as
    # duplicate. itemsPurchased is the more standard name, keep that.
    "itemsViewed",
    "itemsAddedToCart",
    "itemsCheckedOut",
    "itemsPurchased",
    "itemRevenue",
    "cartToViewRate",
    "purchaseToViewRate",
]


def fetch_products(client, since, until):
    logger.info(f"Report 2/3: products ({since} -> {until})")
    rows = run_report(client, PRODUCTS_DIMENSIONS, PRODUCTS_METRICS, since, until)
    logger.info(f"  products rows: {len(rows)}")
    return rows

# ─── PHASE 3: PAGES REPORT ────────────────────────────────────────────────────

PAGES_DIMENSIONS = [
    "date",
    "pagePath",
    # pageTitle removed: it varies per (date, pagePath) for A/B tested titles
    # and creates UPSERT collisions on our (date, brand_id, page_path) key.
    # If we want titles back, run a separate (pagePath, pageTitle) query
    # without date and pick most-common title per page.
]
PAGES_METRICS = [
    # 'entrances' isn't a GA4 metric (UA legacy). We get entrance counts
    # via a separate landingPage report and merge.
    "screenPageViews",
    "sessions",
    "activeUsers",
    "engagedSessions",
    "engagementRate",
    "bounceRate",
    "averageSessionDuration",
    "userEngagementDuration",
]


def fetch_pages(client, since, until):
    """Pages report + landing-page entrance counts merged in."""
    logger.info(f"Report 3/3: pages ({since} -> {until})")
    page_rows = run_report(client, PAGES_DIMENSIONS, PAGES_METRICS, since, until)

    # Second small report: sessions per (date, landingPage) = entrance count.
    landing_rows = run_report(client, ["date", "landingPage"], ["sessions"], since, until)
    entrances = {}
    for r in landing_rows:
        key = (r["dimensions"]["date"], r["dimensions"]["landingPage"])
        entrances[key] = safe_int(r["metrics"]["sessions"])

    # Inject 'entrances' as a synthetic metric on matching pages.
    matched = 0
    for r in page_rows:
        key = (r["dimensions"]["date"], r["dimensions"]["pagePath"])
        if key in entrances:
            matched += 1
        r["metrics"]["entrances"] = str(entrances.get(key, 0))

    logger.info(f"  pages rows: {len(page_rows)} | landing-page entrances merged: {matched}")
    return page_rows

# ─── ROW FORMATTERS ───────────────────────────────────────────────────────────

def format_channel_row(row, now_iso):
    d, m = row["dimensions"], row["metrics"]
    return {
        "date":                     parse_ga4_date(d.get("date")),
        "brand_id":                 BRAND_ID,
        "channel_group":            d.get("sessionDefaultChannelGroup") or "(not set)",
        "source":                   d.get("sessionSource") or "(not set)",
        "medium":                   d.get("sessionMedium") or "(not set)",
        "campaign":                 d.get("sessionCampaignName") or "(not set)",
        "sessions":                 safe_int(m.get("sessions")),
        "engaged_sessions":         safe_int(m.get("engagedSessions")),
        "new_users":                safe_int(m.get("newUsers")),
        "active_users":             safe_int(m.get("activeUsers")),
        "event_count":              safe_int(m.get("eventCount")),
        "engagement_rate":          safe_float(m.get("engagementRate")),
        "average_session_duration": safe_float(m.get("averageSessionDuration")),
        "user_engagement_duration": safe_float(m.get("userEngagementDuration")),
        "bounce_rate":              safe_float(m.get("bounceRate")),
        "key_events":               safe_int(m.get("keyEvents")),
        "add_to_carts":             safe_int(m.get("addToCarts")),
        "checkouts":                safe_int(m.get("checkouts")),
        "ecommerce_purchases":      safe_int(m.get("ecommercePurchases")),
        "total_revenue":            safe_float(m.get("totalRevenue")),
        "purchase_revenue":         safe_float(m.get("purchaseRevenue")),
        "synced_at":                now_iso,
    }


def format_product_row(row, now_iso):
    d, m = row["dimensions"], row["metrics"]
    item_id = d.get("itemId") or "(not set)"
    shopify_product_id, shopify_variant_id = parse_shopify_item_id(item_id)
    return {
        "date":                   parse_ga4_date(d.get("date")),
        "brand_id":               BRAND_ID,
        "item_id":                item_id,
        "item_name":              d.get("itemName"),
        "channel_group":          d.get("sessionDefaultChannelGroup") or "(not set)",
        "shopify_product_id":     shopify_product_id,
        "shopify_variant_id":     shopify_variant_id,
        "item_views":             safe_int(m.get("itemsViewed")),  # GA4 returns item_view count here
        "items_viewed":           safe_int(m.get("itemsViewed")),
        "items_added_to_cart":    safe_int(m.get("itemsAddedToCart")),
        "items_checked_out":      safe_int(m.get("itemsCheckedOut")),
        "items_purchased":        safe_int(m.get("itemsPurchased")),
        "item_purchase_quantity": safe_int(m.get("itemPurchaseQuantity")),
        "item_revenue":           safe_float(m.get("itemRevenue")),
        "cart_to_view_rate":      safe_float(m.get("cartToViewRate")),
        "purchase_to_view_rate":  safe_float(m.get("purchaseToViewRate")),
        "synced_at":              now_iso,
    }


def format_page_row(row, now_iso):
    d, m = row["dimensions"], row["metrics"]
    return {
        "date":                     parse_ga4_date(d.get("date")),
        "brand_id":                 BRAND_ID,
        "page_path":                (d.get("pagePath") or "")[:1024],
        "page_title":               None,  # pageTitle no longer fetched; see PAGES_DIMENSIONS comment
        "screen_page_views":        safe_int(m.get("screenPageViews")),
        "sessions":                 safe_int(m.get("sessions")),
        "entrances":                safe_int(m.get("entrances")),
        "active_users":             safe_int(m.get("activeUsers")),
        "engaged_sessions":         safe_int(m.get("engagedSessions")),
        "engagement_rate":          safe_float(m.get("engagementRate")),
        "bounce_rate":              safe_float(m.get("bounceRate")),
        "average_session_duration": safe_float(m.get("averageSessionDuration")),
        "user_engagement_duration": safe_float(m.get("userEngagementDuration")),
        "synced_at":                now_iso,
    }

# ─── UPSERTS ──────────────────────────────────────────────────────────────────

CHANNELS_UPSERT = """
INSERT INTO ga4_channels_daily (
    date, brand_id,
    channel_group, source, medium, campaign,
    sessions, engaged_sessions, new_users, active_users, event_count,
    engagement_rate, average_session_duration, user_engagement_duration, bounce_rate,
    key_events, add_to_carts, checkouts, ecommerce_purchases,
    total_revenue, purchase_revenue, synced_at
) VALUES (
    %(date)s, %(brand_id)s,
    %(channel_group)s, %(source)s, %(medium)s, %(campaign)s,
    %(sessions)s, %(engaged_sessions)s, %(new_users)s, %(active_users)s, %(event_count)s,
    %(engagement_rate)s, %(average_session_duration)s, %(user_engagement_duration)s, %(bounce_rate)s,
    %(key_events)s, %(add_to_carts)s, %(checkouts)s, %(ecommerce_purchases)s,
    %(total_revenue)s, %(purchase_revenue)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, channel_group, source, medium, campaign) DO UPDATE SET
    sessions                 = EXCLUDED.sessions,
    engaged_sessions         = EXCLUDED.engaged_sessions,
    new_users                = EXCLUDED.new_users,
    active_users             = EXCLUDED.active_users,
    event_count              = EXCLUDED.event_count,
    engagement_rate          = EXCLUDED.engagement_rate,
    average_session_duration = EXCLUDED.average_session_duration,
    user_engagement_duration = EXCLUDED.user_engagement_duration,
    bounce_rate              = EXCLUDED.bounce_rate,
    key_events               = EXCLUDED.key_events,
    add_to_carts             = EXCLUDED.add_to_carts,
    checkouts                = EXCLUDED.checkouts,
    ecommerce_purchases      = EXCLUDED.ecommerce_purchases,
    total_revenue            = EXCLUDED.total_revenue,
    purchase_revenue         = EXCLUDED.purchase_revenue,
    synced_at                = EXCLUDED.synced_at
"""

PRODUCTS_UPSERT = """
INSERT INTO ga4_products_daily (
    date, brand_id,
    item_id, item_name, channel_group,
    shopify_product_id, shopify_variant_id,
    item_views, items_viewed, items_added_to_cart, items_checked_out,
    items_purchased, item_purchase_quantity, item_revenue,
    cart_to_view_rate, purchase_to_view_rate, synced_at
) VALUES (
    %(date)s, %(brand_id)s,
    %(item_id)s, %(item_name)s, %(channel_group)s,
    %(shopify_product_id)s, %(shopify_variant_id)s,
    %(item_views)s, %(items_viewed)s, %(items_added_to_cart)s, %(items_checked_out)s,
    %(items_purchased)s, %(item_purchase_quantity)s, %(item_revenue)s,
    %(cart_to_view_rate)s, %(purchase_to_view_rate)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, item_id, channel_group) DO UPDATE SET
    item_name              = EXCLUDED.item_name,
    shopify_product_id     = EXCLUDED.shopify_product_id,
    shopify_variant_id     = EXCLUDED.shopify_variant_id,
    item_views             = EXCLUDED.item_views,
    items_viewed           = EXCLUDED.items_viewed,
    items_added_to_cart    = EXCLUDED.items_added_to_cart,
    items_checked_out      = EXCLUDED.items_checked_out,
    items_purchased        = EXCLUDED.items_purchased,
    item_purchase_quantity = EXCLUDED.item_purchase_quantity,
    item_revenue           = EXCLUDED.item_revenue,
    cart_to_view_rate      = EXCLUDED.cart_to_view_rate,
    purchase_to_view_rate  = EXCLUDED.purchase_to_view_rate,
    synced_at              = EXCLUDED.synced_at
"""

PAGES_UPSERT = """
INSERT INTO ga4_pages_daily (
    date, brand_id,
    page_path, page_title,
    screen_page_views, sessions, entrances, active_users,
    engaged_sessions, engagement_rate, bounce_rate,
    average_session_duration, user_engagement_duration, synced_at
) VALUES (
    %(date)s, %(brand_id)s,
    %(page_path)s, %(page_title)s,
    %(screen_page_views)s, %(sessions)s, %(entrances)s, %(active_users)s,
    %(engaged_sessions)s, %(engagement_rate)s, %(bounce_rate)s,
    %(average_session_duration)s, %(user_engagement_duration)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, page_path) DO UPDATE SET
    page_title               = EXCLUDED.page_title,
    screen_page_views        = EXCLUDED.screen_page_views,
    sessions                 = EXCLUDED.sessions,
    entrances                = EXCLUDED.entrances,
    active_users             = EXCLUDED.active_users,
    engaged_sessions         = EXCLUDED.engaged_sessions,
    engagement_rate          = EXCLUDED.engagement_rate,
    bounce_rate              = EXCLUDED.bounce_rate,
    average_session_duration = EXCLUDED.average_session_duration,
    user_engagement_duration = EXCLUDED.user_engagement_duration,
    synced_at                = EXCLUDED.synced_at
"""

# ─── SYNC ORCHESTRATION ───────────────────────────────────────────────────────

def write_rows(conn, sql, rows, label):
    """Upsert a batch of dict rows. Per-row try so one bad row doesn't kill the batch."""
    upserted = 0
    errors = 0
    with conn.cursor() as cur:
        for r in rows:
            try:
                if not r.get("date"):
                    continue
                cur.execute(sql, r)
                upserted += 1
            except Exception as e:
                conn.rollback()
                logger.error(f"{label} upsert failed for row {r}: {e}")
                errors += 1
                continue
    conn.commit()
    logger.info(f"Upserted {upserted} rows into {label} (errors: {errors})")
    return upserted, errors


def run_sync(lookback_days, dry_run=False):
    test_db_connection()
    client = get_ga4_client()

    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=lookback_days)).isoformat()
    until = today.isoformat()

    logger.info(f"GA4 sync starting — {since} -> {until} ({lookback_days}-day lookback)")

    channels = fetch_channels(client, since, until)
    products = fetch_products(client, since, until)
    pages    = fetch_pages(client, since, until)

    if dry_run:
        logger.info(
            f"[DRY RUN] Would upsert channels={len(channels)} "
            f"products={len(products)} pages={len(pages)}. No DB writes."
        )
        for label, rows in [("channels", channels), ("products", products), ("pages", pages)]:
            if rows:
                sample = rows[0]
                logger.info(f"  Sample {label}: dims={sample['dimensions']} mets={sample['metrics']}")
        return 0, 0, 0, 0

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    total_errors = 0

    try:
        ch_rows = [format_channel_row(r, now_iso) for r in channels]
        c_up, c_err = write_rows(conn, CHANNELS_UPSERT, ch_rows, "ga4_channels_daily")

        pr_rows = [format_product_row(r, now_iso) for r in products]
        p_up, p_err = write_rows(conn, PRODUCTS_UPSERT, pr_rows, "ga4_products_daily")

        pg_rows = [format_page_row(r, now_iso) for r in pages]
        g_up, g_err = write_rows(conn, PAGES_UPSERT, pg_rows, "ga4_pages_daily")

        total_errors = c_err + p_err + g_err
    finally:
        conn.close()

    logger.info(
        f"Sync complete — channels:{c_up} products:{p_up} pages:{g_up} | errors:{total_errors}"
    )
    return c_up, p_up, g_up, total_errors

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GA4 → Postgres sync")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK,
                        help=f"Days to look back (default {DEFAULT_LOOKBACK})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + summarise without writing to DB")
    args = parser.parse_args()

    c, p, g, errors = run_sync(args.lookback_days, dry_run=args.dry_run)
    logger.info("Script complete")
    sys.exit(1 if errors > 0 else 0)
