"""
gsc_sync.py
===========
Syncs Google Search Console search analytics to PostgreSQL.
Pure data ingestion — no alert logic.

Two reports per run, written daily:
  1. Pages   -> gsc_pages_daily   per (date, page)
  2. Queries -> gsc_queries_daily per (date, query)

Auth via OAuth pickle (gsc_token.pickle), NOT the GA4 service account.
GSC rejects service-account email formats entirely — see
/opt/your_brand_id/GSC_API_Context.md for the why.

Quirks:
  - GSC has 2-3 day data lag, so default lookback is 14d (cron) to catch
    retroactive updates and the lag.
  - GSC anonymises some queries for privacy; reported totals < true totals.
  - 1200 req/min quota — generous; we won't hit it.
  - Max 25,000 rows per response; we paginate via 'startRow' if needed.

Usage:
    python3 gsc_sync.py                       # last 14 days (cron default)
    python3 gsc_sync.py --lookback-days 90    # wider backfill
    python3 gsc_sync.py --dry-run             # fetch + summarise, no writes
"""

import os
import sys
import pickle
import logging
import argparse
from datetime import datetime, timedelta, timezone
import psycopg2
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv("/opt/your_brand_id/.env")

GSC_TOKEN_FILE   = os.getenv("GSC_TOKEN_FILE", "/opt/your_brand_id/credentials/gsc_token.pickle")
GSC_SITE_URL     = os.getenv("GSC_SITE_URL", "sc-domain:your-domain.com")

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

BRAND_ID         = os.getenv("BRAND_ID", "your_brand_id")
DEFAULT_LOOKBACK = int(os.getenv("GSC_LOOKBACK_DAYS", "14"))
LOG_FILE         = os.getenv("GSC_LOG_FILE", "logs/gsc_sync.log")

MAX_ROWS_PER_PAGE = 25000

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)

logger = logging.getLogger("gsc_sync")
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

# ─── GSC CLIENT ───────────────────────────────────────────────────────────────

def get_gsc_service():
    """Load the OAuth pickle, refresh if expired, return a Search Console v1
    service client. Refresh token is in the pickle (Internal OAuth app =
    never expires)."""
    if not os.path.exists(GSC_TOKEN_FILE):
        logger.critical(f"GSC token file not found: {GSC_TOKEN_FILE}")
        sys.exit(1)
    with open(GSC_TOKEN_FILE, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        logger.info("Refreshing GSC access token")
        creds.refresh(Request())
        # Persist refreshed creds so the next run has the up-to-date access token
        try:
            with open(GSC_TOKEN_FILE, "wb") as f:
                pickle.dump(creds, f)
        except Exception as e:
            logger.warning(f"Could not save refreshed token (continuing): {e}")
    return build("searchconsole", "v1", credentials=creds)

# ─── QUERY HELPER ─────────────────────────────────────────────────────────────

def run_search_analytics(service, dimensions, since, until):
    """Paginate searchanalytics.query for the given dimensions + window.

    Returns a list of rows, each row is the raw GSC response dict like:
      {'keys': [...dim values...], 'clicks': N, 'impressions': N, 'ctr': N, 'position': N}
    """
    all_rows = []
    start_row = 0
    while True:
        body = {
            "startDate":   since.isoformat(),
            "endDate":     until.isoformat(),
            "dimensions":  dimensions,
            "rowLimit":    MAX_ROWS_PER_PAGE,
            "startRow":    start_row,
            "type":        "web",
        }
        try:
            resp = service.searchanalytics().query(
                siteUrl=GSC_SITE_URL, body=body,
            ).execute()
        except HttpError as e:
            logger.error(f"GSC API error (dims={dimensions} start={start_row}): {e}")
            raise

        rows = resp.get("rows", []) or []
        all_rows.extend(rows)
        if len(rows) < MAX_ROWS_PER_PAGE:
            break
        start_row += MAX_ROWS_PER_PAGE
    return all_rows

# ─── FORMATTERS ───────────────────────────────────────────────────────────────

def format_page_row(row, now_iso):
    """GSC returns 'keys': ['<date>', '<page>'] when dimensions=['date','page']."""
    keys = row.get("keys") or [None, None]
    return {
        "date":        keys[0],
        "brand_id":    BRAND_ID,
        "page":        (keys[1] or "")[:2048],
        "clicks":      int(row.get("clicks") or 0),
        "impressions": int(row.get("impressions") or 0),
        "ctr":         float(row.get("ctr") or 0),
        "position":    float(row.get("position") or 0),
        "synced_at":   now_iso,
    }


def format_query_row(row, now_iso):
    """GSC returns 'keys': ['<date>', '<query>'] when dimensions=['date','query']."""
    keys = row.get("keys") or [None, None]
    return {
        "date":        keys[0],
        "brand_id":    BRAND_ID,
        "query":       (keys[1] or "")[:2048],
        "clicks":      int(row.get("clicks") or 0),
        "impressions": int(row.get("impressions") or 0),
        "ctr":         float(row.get("ctr") or 0),
        "position":    float(row.get("position") or 0),
        "synced_at":   now_iso,
    }

# ─── UPSERTS ──────────────────────────────────────────────────────────────────

PAGES_UPSERT = """
INSERT INTO gsc_pages_daily (
    date, brand_id, page,
    clicks, impressions, ctr, position, synced_at
) VALUES (
    %(date)s, %(brand_id)s, %(page)s,
    %(clicks)s, %(impressions)s, %(ctr)s, %(position)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, page) DO UPDATE SET
    clicks      = EXCLUDED.clicks,
    impressions = EXCLUDED.impressions,
    ctr         = EXCLUDED.ctr,
    position    = EXCLUDED.position,
    synced_at   = EXCLUDED.synced_at
"""

QUERIES_UPSERT = """
INSERT INTO gsc_queries_daily (
    date, brand_id, query,
    clicks, impressions, ctr, position, synced_at
) VALUES (
    %(date)s, %(brand_id)s, %(query)s,
    %(clicks)s, %(impressions)s, %(ctr)s, %(position)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, query) DO UPDATE SET
    clicks      = EXCLUDED.clicks,
    impressions = EXCLUDED.impressions,
    ctr         = EXCLUDED.ctr,
    position    = EXCLUDED.position,
    synced_at   = EXCLUDED.synced_at
"""


def write_rows(conn, sql, rows, label):
    upserted = errors = 0
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

# ─── SYNC ─────────────────────────────────────────────────────────────────────

def run_sync(lookback_days, dry_run=False):
    test_db_connection()
    service = get_gsc_service()

    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=lookback_days)
    until = today

    logger.info(
        f"GSC sync starting — site={GSC_SITE_URL} window {since} -> {until} "
        f"({lookback_days}-day lookback)"
    )

    logger.info("Report 1/2: pages (dimensions=date,page)")
    page_rows = run_search_analytics(service, ["date", "page"], since, until)
    logger.info(f"  pages rows: {len(page_rows)}")

    logger.info("Report 2/2: queries (dimensions=date,query)")
    query_rows = run_search_analytics(service, ["date", "query"], since, until)
    logger.info(f"  queries rows: {len(query_rows)}")

    if dry_run:
        logger.info(
            f"[DRY RUN] Would upsert pages={len(page_rows)} queries={len(query_rows)}. No DB writes."
        )
        if page_rows:
            logger.info(f"  Sample page row: {page_rows[0]}")
        if query_rows:
            logger.info(f"  Sample query row: {query_rows[0]}")
        return 0, 0, 0

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        p_rows = [format_page_row(r, now_iso) for r in page_rows]
        p_up, p_err = write_rows(conn, PAGES_UPSERT, p_rows, "gsc_pages_daily")

        q_rows = [format_query_row(r, now_iso) for r in query_rows]
        q_up, q_err = write_rows(conn, QUERIES_UPSERT, q_rows, "gsc_queries_daily")

        total_errors = p_err + q_err
    finally:
        conn.close()

    logger.info(f"Sync complete — pages:{p_up} queries:{q_up} | errors:{total_errors}")
    return p_up, q_up, total_errors

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Google Search Console → Postgres sync")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK,
                        help=f"Days to look back (default {DEFAULT_LOOKBACK})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + summarise without writing to DB")
    args = parser.parse_args()

    p, q, errors = run_sync(args.lookback_days, dry_run=args.dry_run)
    logger.info("Script complete")
    sys.exit(1 if errors > 0 else 0)
