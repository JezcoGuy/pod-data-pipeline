"""
ga4_sessions_backfill.py
========================
Ingest pre-GA4 session/funnel metrics from the Your Brand daily-stats CSV
into ga4_sessions_backfill. Source = Shopify Analytics spreadsheet
manually tracked Jul 2025 → May 8 2026 (the day before live GA4 sync
started). Each backfill row is marked data_source='shopify_analytics' so
v_sessions_daily can distinguish it from live GA4 rows downstream.

Idempotent via UNIQUE (date, brand_id) + ON CONFLICT DO NOTHING — safe to
re-run.

Usage:
  python3 ga4_sessions_backfill.py --dry-run    # parse + summarise, no DB writes
  python3 ga4_sessions_backfill.py              # ingest
"""

import argparse
import csv
import logging
import os
import sys
from datetime import date as date_t, datetime

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv("/opt/your_brand_id/.env", override=True)

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
BRAND_ID    = os.getenv("BRAND_ID", "your_brand_id")

# Filename has spaces in the actual upload — keep it verbatim.
CSV_PATH    = os.getenv(
    "GA4_BACKFILL_CSV",
    "/opt/your_brand_id/data/backfill/Your Brand Summary - Daily Stats.csv",
)
LOG_FILE    = os.getenv("GA4_BACKFILL_LOG", "logs/ga4_sessions_backfill.log")
GA4_LIVE_FROM = date_t(2026, 5, 9)   # ingest only rows strictly before this

# Column indices (0-based) per brief Section 2
COL_DATE     = 0
COL_CR_PCT   = 13
COL_DESIGNS  = 25
COL_ADS      = 26
COL_EMAILS   = 27
COL_ORDERS   = 32
COL_SESSIONS = 34
COL_ATC      = 35
COL_ATC_PCT  = 36
COL_RC       = 37
COL_IC_PCT   = 38
COL_RTN      = 88
COL_RTN_PCT  = 90

# ─── logging ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)
logger = logging.getLogger("ga4_sessions_backfill")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s [ga4_sessions_backfill] [%(levelname)s] %(message)s"))
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(fh)
logger.addHandler(sh)

# ─── helpers ──────────────────────────────────────────────────────────────────

def cell(row, idx):
    return row[idx] if len(row) > idx else ""


def safe_float(val):
    if val is None:
        return None
    s = str(val).strip().replace("%", "").replace("£", "").replace(",", "")
    if not s or s in ("#DIV/0!", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def safe_int(val):
    f = safe_float(val)
    return int(f) if f is not None else None


def parse_csv(path):
    """Yield validated row dicts ready for upsert."""
    rows_in   = 0
    skipped   = {"non_data": 0, "bad_date": 0, "no_sessions": 0, "after_ga4": 0}
    out       = []

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        all_rows = list(reader)

    logger.info(f"  raw lines in file: {len(all_rows)}")

    for raw in all_rows[9:]:   # data starts at row 10 (0-indexed: 9)
        if len(raw) < 40:
            skipped["non_data"] += 1
            continue
        rows_in += 1

        date_str = cell(raw, COL_DATE).strip()
        if not date_str or any(x in date_str.upper() for x in ("DAILY", "TOTAL", "DATE")):
            skipped["non_data"] += 1
            continue

        try:
            d = datetime.strptime(date_str, "%A, %b %d, %Y").date()
        except ValueError:
            skipped["bad_date"] += 1
            continue

        if d >= GA4_LIVE_FROM:
            skipped["after_ga4"] += 1
            continue

        sessions = safe_int(cell(raw, COL_SESSIONS))
        if not sessions:
            skipped["no_sessions"] += 1
            continue

        out.append({
            "date":                 d,
            "sessions":             sessions,
            "atc":                  safe_int  (cell(raw, COL_ATC)),
            "atc_rate_pct":         safe_float(cell(raw, COL_ATC_PCT)),
            "reached_checkout":     safe_int  (cell(raw, COL_RC)),
            "reached_checkout_pct": safe_float(cell(raw, COL_IC_PCT)),
            "purchases":            safe_int  (cell(raw, COL_ORDERS)),
            "cr_pct":               safe_float(cell(raw, COL_CR_PCT)),
            "returning_orders":     safe_int  (cell(raw, COL_RTN)),
            "returning_pct":        safe_float(cell(raw, COL_RTN_PCT)),
            "designs_uploaded":     safe_int  (cell(raw, COL_DESIGNS)),
            "ads_launched":         safe_int  (cell(raw, COL_ADS)),
            "emails_sent":          safe_int  (cell(raw, COL_EMAILS)),
        })

    logger.info(f"  candidate data rows iterated: {rows_in}")
    logger.info(f"  skipped: {skipped}")
    return out

# ─── DB ───────────────────────────────────────────────────────────────────────

UPSERT = """
INSERT INTO ga4_sessions_backfill (
    date, brand_id, data_source,
    sessions, atc, atc_rate_pct,
    reached_checkout, reached_checkout_pct,
    purchases, cr_pct,
    returning_orders, returning_pct,
    designs_uploaded, ads_launched, emails_sent
) VALUES (
    %(date)s, %(brand_id)s, 'shopify_analytics',
    %(sessions)s, %(atc)s, %(atc_rate_pct)s,
    %(reached_checkout)s, %(reached_checkout_pct)s,
    %(purchases)s, %(cr_pct)s,
    %(returning_orders)s, %(returning_pct)s,
    %(designs_uploaded)s, %(ads_launched)s, %(emails_sent)s
) ON CONFLICT (date, brand_id) DO NOTHING;
"""


def db_connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)


def ingest(conn, rows, dry_run):
    if dry_run or not rows:
        return len(rows), 0

    # Detect existing rows so we can report inserted vs skipped accurately.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date FROM ga4_sessions_backfill WHERE brand_id = %s AND date = ANY(%s)",
            (BRAND_ID, [r["date"] for r in rows]),
        )
        existing = {d for (d,) in cur.fetchall()}

    to_insert = [dict(r, brand_id=BRAND_ID) for r in rows if r["date"] not in existing]
    skipped   = len(rows) - len(to_insert)

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, UPSERT, to_insert, page_size=200)
    conn.commit()
    return len(to_insert), skipped


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Parse + summarise, no DB writes.")
    args = p.parse_args()

    mode = "DRY RUN" if args.dry_run else "EXECUTE"
    logger.info(f"ga4_sessions_backfill starting — {mode}")
    logger.info(f"  CSV: {CSV_PATH}")
    logger.info(f"  cutoff (ingest rows BEFORE this date): {GA4_LIVE_FROM}")

    rows = parse_csv(CSV_PATH)
    if not rows:
        logger.error("No valid rows parsed — aborting.")
        sys.exit(1)

    logger.info("")
    logger.info(f"Parsed {len(rows)} valid rows from CSV")
    logger.info(f"Date range: {rows[0]['date']} → {rows[-1]['date']}")
    logger.info(f"Sample row (first): {rows[0]}")
    logger.info(f"Sample row (last):  {rows[-1]}")

    conn = db_connect()
    try:
        inserted, skipped = ingest(conn, rows, args.dry_run)
    finally:
        conn.close()

    logger.info("")
    logger.info(f"{'Would insert' if args.dry_run else 'Inserted'}: {inserted}")
    logger.info(f"Skipped (already in DB): {skipped}")

    if args.dry_run:
        logger.info("")
        logger.info("Dry run complete. No DB writes. Re-run without --dry-run to apply.")


if __name__ == "__main__":
    main()
