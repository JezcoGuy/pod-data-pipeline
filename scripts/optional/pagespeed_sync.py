"""
pagespeed_sync.py
=================
Daily Lighthouse audit snapshots for key Your Brand URLs via Google's
PageSpeed Insights API. One row per (date, page_url, strategy) into
pagespeed_daily.

For each URL, two API calls (mobile + desktop). Each call takes ~20-40 sec
because Lighthouse actually runs the audit against the live site. Total
runtime for 4 URLs ≈ 4 minutes. API key is IP-restricted to the server.

What URLs to audit? Defaults to homepage + key collections + a sample
product page. Override via env PAGESPEED_URLS (comma-separated).

Usage:
    python3 pagespeed_sync.py
    python3 pagespeed_sync.py --dry-run
    python3 pagespeed_sync.py --url https://your-domain.com/  # one URL only
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
import psycopg2
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv()

PAGESPEED_API_KEY  = os.getenv("PAGESPEED_API_KEY")
PAGESPEED_URLS_ENV = os.getenv("PAGESPEED_URLS")

# Default URLs to audit if PAGESPEED_URLS isn't set. Tweak in .env if your
# focus shifts (e.g. swap in new hero designs).
DEFAULT_URLS = [
    "https://your-domain.com/",
    "https://your-domain.com/collections/best-sellers",
    "https://your-domain.com/collections/all-products",
    "https://your-domain.com/products/guitar-problem-t-shirt",
]

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

BRAND_ID         = os.getenv("BRAND_ID", "your_brand_id")
REQUEST_TIMEOUT  = int(os.getenv("PAGESPEED_TIMEOUT", "180"))
LOG_FILE         = os.getenv("PAGESPEED_LOG_FILE", "logs/pagespeed_sync.log")

API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)

logger = logging.getLogger("pagespeed_sync")
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

# ─── PAGESPEED API ────────────────────────────────────────────────────────────

CATEGORIES = ["performance", "accessibility", "best-practices", "seo"]


def fetch_pagespeed(url, strategy, max_retries=3):
    """One PageSpeed Insights API call. Returns the full JSON body or raises."""
    params = [("url", url), ("strategy", strategy), ("key", PAGESPEED_API_KEY)]
    # PageSpeed accepts repeated category= params
    for cat in CATEGORIES:
        params.append(("category", cat))

    backoff = 5
    for attempt in range(max_retries):
        try:
            resp = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", backoff))
                logger.warning(f"PageSpeed 429 — retry in {wait}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 500:
                logger.warning(f"PageSpeed {resp.status_code} (attempt {attempt+1}) — retry in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code != 200:
                logger.error(f"PageSpeed {resp.status_code} for {url} {strategy}: {resp.text[:300]}")
                resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            logger.warning(f"PageSpeed timeout for {url} {strategy} — retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"PageSpeed connection error: {e} — retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    raise Exception(f"PageSpeed failed after {max_retries} attempts: {url} {strategy}")

# ─── PARSING ──────────────────────────────────────────────────────────────────

def _score(categories, name):
    """Extract category score, multiply by 100, round to int. None if absent."""
    c = (categories or {}).get(name) or {}
    s = c.get("score")
    return int(round(s * 100)) if isinstance(s, (int, float)) else None


def _audit_num(audits, key):
    """numericValue of a Lighthouse audit, or None."""
    a = (audits or {}).get(key) or {}
    v = a.get("numericValue")
    return float(v) if isinstance(v, (int, float)) else None


def parse_response(body, url, strategy, now_iso):
    """Map a PageSpeed JSON response into a pagespeed_daily row dict."""
    lh = body.get("lighthouseResult") or {}
    cats = lh.get("categories") or {}
    audits = lh.get("audits") or {}
    fetched_at = lh.get("fetchTime") or now_iso  # ISO string already

    path = urlparse(url).path or "/"

    return {
        "date":                 datetime.now(timezone.utc).date(),
        "brand_id":             BRAND_ID,
        "page_url":             url,
        "page_path":            path[:1024],
        "strategy":             strategy,

        "score_performance":    _score(cats, "performance"),
        "score_accessibility":  _score(cats, "accessibility"),
        "score_best_practices": _score(cats, "best-practices"),
        "score_seo":            _score(cats, "seo"),

        "lcp_ms":               _audit_num(audits, "largest-contentful-paint"),
        "cls":                  _audit_num(audits, "cumulative-layout-shift"),
        "inp_ms":               _audit_num(audits, "interaction-to-next-paint"),
        "fcp_ms":               _audit_num(audits, "first-contentful-paint"),
        "ttfb_ms":              _audit_num(audits, "server-response-time"),
        "tbt_ms":               _audit_num(audits, "total-blocking-time"),
        "speed_index_ms":       _audit_num(audits, "speed-index"),

        "fetched_at":           fetched_at,
        "synced_at":            now_iso,
    }

# ─── UPSERT ───────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO pagespeed_daily (
    date, brand_id, page_url, page_path, strategy,
    score_performance, score_accessibility, score_best_practices, score_seo,
    lcp_ms, cls, inp_ms, fcp_ms, ttfb_ms, tbt_ms, speed_index_ms,
    fetched_at, synced_at
) VALUES (
    %(date)s, %(brand_id)s, %(page_url)s, %(page_path)s, %(strategy)s,
    %(score_performance)s, %(score_accessibility)s, %(score_best_practices)s, %(score_seo)s,
    %(lcp_ms)s, %(cls)s, %(inp_ms)s, %(fcp_ms)s, %(ttfb_ms)s, %(tbt_ms)s, %(speed_index_ms)s,
    %(fetched_at)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, page_url, strategy) DO UPDATE SET
    page_path            = EXCLUDED.page_path,
    score_performance    = EXCLUDED.score_performance,
    score_accessibility  = EXCLUDED.score_accessibility,
    score_best_practices = EXCLUDED.score_best_practices,
    score_seo            = EXCLUDED.score_seo,
    lcp_ms               = EXCLUDED.lcp_ms,
    cls                  = EXCLUDED.cls,
    inp_ms               = EXCLUDED.inp_ms,
    fcp_ms               = EXCLUDED.fcp_ms,
    ttfb_ms              = EXCLUDED.ttfb_ms,
    tbt_ms               = EXCLUDED.tbt_ms,
    speed_index_ms       = EXCLUDED.speed_index_ms,
    fetched_at           = EXCLUDED.fetched_at,
    synced_at            = EXCLUDED.synced_at
"""

# ─── ORCHESTRATION ────────────────────────────────────────────────────────────

def get_urls():
    if PAGESPEED_URLS_ENV:
        urls = [u.strip() for u in PAGESPEED_URLS_ENV.split(",") if u.strip()]
        if urls:
            return urls
    return DEFAULT_URLS


def run_sync(single_url=None, dry_run=False):
    if not PAGESPEED_API_KEY:
        logger.critical("PAGESPEED_API_KEY missing from .env")
        sys.exit(1)
    test_db_connection()

    urls = [single_url] if single_url else get_urls()
    strategies = ["mobile", "desktop"]
    now_iso = datetime.now(timezone.utc).isoformat()

    logger.info(f"PageSpeed sync — {len(urls)} URLs × {len(strategies)} strategies = {len(urls)*len(strategies)} audits")

    rows = []
    errors = 0
    for url in urls:
        for strategy in strategies:
            logger.info(f"  auditing {url} ({strategy})")
            try:
                body = fetch_pagespeed(url, strategy)
                row = parse_response(body, url, strategy, now_iso)
                rows.append(row)
                logger.info(
                    f"    perf={row['score_performance']} a11y={row['score_accessibility']} "
                    f"best={row['score_best_practices']} seo={row['score_seo']} "
                    f"LCP={row['lcp_ms']:.0f}ms CLS={row['cls']}"
                    if row['score_performance'] is not None else "    (no scores returned)"
                )
            except Exception as e:
                logger.error(f"    failed: {e}")
                errors += 1

    if dry_run:
        logger.info(f"[DRY RUN] Would upsert {len(rows)} rows. No DB writes.")
        return 0, errors

    if not rows:
        logger.warning("No rows to write")
        return 0, errors

    conn = get_db_connection()
    upserted = 0
    try:
        with conn.cursor() as cur:
            for row in rows:
                try:
                    cur.execute(UPSERT_SQL, row)
                    upserted += 1
                except Exception as e:
                    conn.rollback()
                    logger.error(f"upsert failed for {row.get('page_url')} {row.get('strategy')}: {e}")
                    errors += 1
                    continue
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Sync complete — upserted: {upserted} | errors: {errors}")
    return upserted, errors

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PageSpeed Insights → Postgres sync")
    parser.add_argument("--url", type=str, default=None,
                        help="Audit only this URL (overrides PAGESPEED_URLS and defaults)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + summarise without writing to DB")
    args = parser.parse_args()

    upserted, errors = run_sync(single_url=args.url, dry_run=args.dry_run)
    logger.info("Script complete")
    sys.exit(1 if errors > 0 else 0)
