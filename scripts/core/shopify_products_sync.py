"""
shopify_products_sync.py
========================
Syncs Shopify product catalogue to PostgreSQL.
Pure data ingestion — no alert logic.

Fetches active products (with variants) from Shopify Admin API 2025-04 and
upserts into product_catalogue. Preserves default_cogs_gbp and gelato_product_id
(set manually or by other scripts). After a clean run, any rows not touched
this run are flipped to active=FALSE — that's how archived products get
flagged without losing the row (preserves historical FK references from
ad_campaign_products etc.).

Usage:
    python3 shopify_products_sync.py
    python3 shopify_products_sync.py --dry-run
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, timezone
import requests
import psycopg2
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv("/opt/your_brand_id/.env")

SHOPIFY_ACCESS_TOKEN = os.getenv('SHOPIFY_ACCESS_TOKEN')
SHOPIFY_STORE        = os.getenv('SHOPIFY_STORE_NAME')

DB_HOST     = os.getenv('DB_HOST', 'localhost')
DB_PORT     = os.getenv('DB_PORT', '5432')
DB_NAME     = os.getenv('DB_NAME')
DB_USER     = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')

BRAND_ID        = os.getenv('BRAND_ID', 'your_brand_id')
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '30'))
LOG_FILE        = os.getenv('PRODUCTS_LOG_FILE', 'logs/shopify_products_sync.log')

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else '.', exist_ok=True)

logger = logging.getLogger('shopify_products_sync')
logger.setLevel(logging.DEBUG)
fmt = logging.Formatter('%(asctime)s [%(name)s] [%(levelname)s] %(message)s')

fh = logging.FileHandler(LOG_FILE)
fh.setLevel(logging.INFO)
fh.setFormatter(fmt)
logger.addHandler(fh)

ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(fmt)
logger.addHandler(ch)

# ─── POSTGRES ─────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD
    )
    conn.autocommit = False
    return conn


def test_db_connection():
    try:
        conn = get_db_connection()
        conn.close()
        logger.info('Postgres connection OK')
    except Exception as e:
        logger.critical(f'Postgres connection failed: {e}')
        sys.exit(1)

# ─── SHOPIFY API ──────────────────────────────────────────────────────────────

BASE_URL          = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2025-04"
PRODUCTS_ENDPOINT = f"{BASE_URL}/products.json"
HEADERS           = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN}


def request_with_retry(url, params=None, max_retries=3):
    """GET with rate limit handling, exponential backoff and 5xx retry."""
    logger.debug(f'GET {url} params={params}')
    backoff = 1
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                retry = int(resp.headers.get('Retry-After', backoff))
                logger.warning(f'Rate limited — retrying after {retry}s')
                time.sleep(retry)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 500:
                logger.warning(f'Shopify {resp.status_code} — retrying in {backoff}s (attempt {attempt+1})')
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            resp.raise_for_status()
            limit = resp.headers.get('X-Shopify-Shop-Api-Call-Limit')
            if limit:
                used, total = map(int, limit.split('/'))
                logger.debug(f'API usage {used}/{total}')
                time.sleep(5 if used / total >= 0.8 else 0.3)
            else:
                time.sleep(0.3)
            return resp
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            # ReadTimeout/ConnectTimeout treated the same as a dropped
            # connection — transient, retry with backoff. Shopify's
            # /products.json second page intermittently exceeds the 30s
            # read timeout (cron.log 2026-06-10 02:30); without this
            # the whole sync crashes on a single slow response.
            logger.warning(f'Network error — retrying in {backoff}s: {e}')
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    raise Exception(f'Failed after {max_retries} attempts: {url}')


def fetch_active_products():
    """Fetch all active products with variants. Paginates via Link header."""
    logger.info('Fetching active products from Shopify')
    params = {'status': 'active', 'limit': 250}
    url    = PRODUCTS_ENDPOINT
    products = []

    while url:
        resp  = request_with_retry(url, params=params)
        batch = resp.json().get('products', [])
        logger.debug(f'Batch: {len(batch)} products')
        products.extend(batch)

        link = resp.headers.get('Link', '')
        if 'rel="next"' not in link:
            break
        parts = [p.split(';') for p in link.split(',')]
        url   = next(
            (p[0].strip('<> ') for p in parts if len(p) > 1 and 'rel="next"' in p[1]),
            None
        )
        params = None  # params embedded in next URL

    variant_count = sum(len(p.get('variants', [])) for p in products)
    logger.info(f'Total products: {len(products)} | total variants: {variant_count}')
    return products

# ─── DATABASE OPERATIONS ──────────────────────────────────────────────────────

UPSERT_SQL = """
    INSERT INTO product_catalogue (
        product_id, variant_id, brand_id,
        product_title, product_type, variant_title, product_handle,
        product_created_at, product_tags,
        active, synced_at
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
    ON CONFLICT (product_id, variant_id, brand_id) DO UPDATE SET
        product_title      = EXCLUDED.product_title,
        product_type       = EXCLUDED.product_type,
        variant_title      = EXCLUDED.variant_title,
        product_handle     = EXCLUDED.product_handle,
        product_created_at = COALESCE(EXCLUDED.product_created_at, product_catalogue.product_created_at),
        product_tags       = EXCLUDED.product_tags,
        active             = TRUE,
        synced_at          = EXCLUDED.synced_at
"""


def upsert_variant(cur, product, variant, synced_at):
    """Upsert one (product, variant) row.
    Preserves default_cogs_gbp and gelato_product_id (not in column list)."""
    cur.execute(UPSERT_SQL, (
        str(product['id']),
        str(variant['id']),
        BRAND_ID,
        product.get('title'),
        product.get('product_type'),
        variant.get('title'),
        product.get('handle'),
        product.get('created_at'),
        product.get('tags') or None,    # Shopify returns '' when no tags; normalise to NULL
        synced_at,
    ))


def sweep_archived(cur, run_started_at):
    """Flip active=FALSE on any row not seen in this run.

    Uses synced_at as the 'was touched this run' marker — any row with
    synced_at older than the run start (or NULL) wasn't refreshed and is
    therefore no longer in Shopify's active list. Only call this after a
    clean sync (errors == 0) so a partial sync doesn't deactivate rows
    incorrectly.
    """
    cur.execute("""
        UPDATE product_catalogue
        SET active = FALSE
        WHERE brand_id = %s
          AND active = TRUE
          AND (synced_at < %s OR synced_at IS NULL)
    """, (BRAND_ID, run_started_at))
    return cur.rowcount

# ─── SYNC ─────────────────────────────────────────────────────────────────────

def run_sync(dry_run=False):
    test_db_connection()

    products       = fetch_active_products()
    run_started_at = datetime.now(timezone.utc).isoformat()

    if dry_run:
        variants = sum(len(p.get('variants', [])) for p in products)
        logger.info(f'[DRY RUN] Would upsert {variants} variants from {len(products)} products')
        for p in products[:3]:
            logger.info(
                f'  Example: handle={p.get("handle")!r} '
                f'title={p.get("title")!r} '
                f'variants={len(p.get("variants", []))}'
            )
        return 0, 0, 0

    conn      = get_db_connection()
    upserted  = 0
    errors    = 0
    archived  = 0

    try:
        with conn.cursor() as cur:
            for product in products:
                try:
                    for variant in product.get('variants', []):
                        upsert_variant(cur, product, variant, run_started_at)
                        upserted += 1
                    conn.commit()
                    logger.debug(
                        f'Synced {product.get("title")} '
                        f'({len(product.get("variants", []))} variants)'
                    )
                except Exception as e:
                    conn.rollback()
                    logger.error(
                        f'Failed product {product.get("id")} '
                        f'({product.get("title")}): {e}'
                    )
                    errors += 1

            if errors == 0:
                archived = sweep_archived(cur, run_started_at)
                conn.commit()
                logger.info(f'Archive sweep: {archived} rows flipped active=FALSE')
            else:
                logger.warning(
                    f'Skipping archive sweep — {errors} product(s) failed this run'
                )

    finally:
        conn.close()

    logger.info(
        f'Sync complete — variants upserted: {upserted} | '
        f'errors: {errors} | archived: {archived}'
    )
    return upserted, errors, archived

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Shopify products → Postgres sync')
    parser.add_argument('--dry-run', action='store_true',
                        help='Fetch and summarise without writing to DB')
    args = parser.parse_args()

    logger.info('Shopify products sync starting')
    upserted, errors, archived = run_sync(dry_run=args.dry_run)
    logger.info('Script complete')
    sys.exit(1 if errors > 0 else 0)
