#!/usr/bin/env python3
"""
ad_discovery_cleanup.py
=======================
Daily script — removes products older than 45 days from:
  1. New Arrivals Shopify collection (manual)
  2. ad_discovery tag (smart collection auto-updates)

Runs at 03:45 via cron — after product sync (02:30), before nightly alert (04:00).
Logs all changes to ad_discovery_cleanup_log for nightly alert summary.

Usage:
  python3 scripts/ad_discovery_cleanup.py
  python3 scripts/ad_discovery_cleanup.py --dry-run   # preview only, no changes
"""

import os
import sys
import logging
import argparse
import time
import psycopg2
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# ── Config ────────────────────────────────────────────────────────────────────
BRAND_ID        = os.getenv('BRAND_ID', 'your_brand_id')
SHOPIFY_STORE   = os.getenv('SHOPIFY_STORE_NAME')
SHOPIFY_TOKEN   = os.getenv('SHOPIFY_ACCESS_TOKEN')
COLLECTION_ID   = os.getenv('SHOPIFY_NEW_ARRIVALS_COLLECTION_ID')
AGE_THRESHOLD   = int(os.getenv('AD_DISCOVERY_DAYS', '45'))

DB_CONFIG = {
    'host':     os.getenv('DB_HOST', 'localhost'),
    'port':     int(os.getenv('DB_PORT', 5432)),
    'dbname':   os.getenv('DB_NAME'),
    'user':     os.getenv('DB_USER'),
    'password': os.getenv('DB_PASSWORD'),
}

SHOPIFY_BASE = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2024-01"
HEADERS = {
    'X-Shopify-Access-Token': SHOPIFY_TOKEN,
    'Content-Type': 'application/json',
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ad_discovery_cleanup] [%(levelname)s] %(message)s'
)
logger = logging.getLogger('ad_discovery_cleanup')


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(**DB_CONFIG)


def get_products_to_clean(conn):
    """Get distinct products with ad_discovery tag older than threshold."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (product_handle)
                product_id,
                product_handle,
                product_title,
                product_tags,
                (CURRENT_DATE - product_created_at::date) AS age_days
            FROM product_catalogue
            WHERE brand_id = %s
              AND product_tags ILIKE '%%ad_discovery%%'
              AND (CURRENT_DATE - product_created_at::date) > %s
            ORDER BY product_handle, product_created_at ASC
        """, (BRAND_ID, AGE_THRESHOLD))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def log_result(conn, product_id, product_handle, product_title,
               age_days, action_collection, action_tag, error=None):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ad_discovery_cleanup_log (
                brand_id, product_id, product_handle, product_title,
                age_days, action_collection, action_tag, error
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (BRAND_ID, product_id, product_handle, product_title,
              age_days, action_collection, action_tag, error))
    conn.commit()


# ── Shopify helpers ───────────────────────────────────────────────────────────

def remove_from_collection(product_id, dry_run=False):
    """Remove product from New Arrivals collection."""
    if not COLLECTION_ID:
        logger.warning('SHOPIFY_NEW_ARRIVALS_COLLECTION_ID not set — skipping collection removal')
        return False

    # Find the collect record
    r = requests.get(
        f"{SHOPIFY_BASE}/collects.json",
        headers=HEADERS,
        params={'product_id': product_id, 'collection_id': COLLECTION_ID}
    )
    r.raise_for_status()
    collects = r.json().get('collects', [])

    if not collects:
        logger.info(f'  Product {product_id} not in New Arrivals collection — skipping')
        return False

    collect_id = collects[0]['id']

    if dry_run:
        logger.info(f'  [DRY RUN] Would remove collect {collect_id} from New Arrivals')
        return True

    r = requests.delete(
        f"{SHOPIFY_BASE}/collects/{collect_id}.json",
        headers=HEADERS
    )
    r.raise_for_status()
    logger.info(f'  Removed from New Arrivals (collect_id={collect_id})')
    time.sleep(0.5)  # Rate limit respect
    return True


def remove_tag(product_id, current_tags, dry_run=False):
    """Remove ad_discovery tag from product."""
    tags_list = [t.strip() for t in current_tags.split(',') if t.strip()]
    new_tags = [t for t in tags_list if t.lower() != 'ad_discovery']

    if len(new_tags) == len(tags_list):
        logger.info(f'  ad_discovery tag not found on product {product_id}')
        return False

    new_tags_str = ', '.join(new_tags)

    if dry_run:
        logger.info(f'  [DRY RUN] Would update tags: removed ad_discovery')
        return True

    r = requests.put(
        f"{SHOPIFY_BASE}/products/{product_id}.json",
        headers=HEADERS,
        json={'product': {'id': product_id, 'tags': new_tags_str}}
    )
    r.raise_for_status()
    logger.info(f'  Removed ad_discovery tag — {len(tags_list) - len(new_tags)} tag(s) removed')
    time.sleep(0.5)  # Rate limit respect
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main(dry_run=False):
    logger.info(f'Ad discovery cleanup starting — threshold: {AGE_THRESHOLD} days'
                + (' [DRY RUN]' if dry_run else ''))

    conn = get_db()
    products = get_products_to_clean(conn)

    logger.info(f'Found {len(products)} products to clean up')

    if not products:
        logger.info('Nothing to do — exiting')
        conn.close()
        return

    cleaned = 0
    errors  = 0

    for p in products:
        logger.info(f"Processing: {p['product_title']} "
                    f"(age={p['age_days']}d, id={p['product_id']})")

        action_collection = False
        action_tag        = False
        error             = None

        try:
            action_collection = remove_from_collection(p['product_id'], dry_run)
            action_tag        = remove_tag(
                p['product_id'], p['product_tags'], dry_run
            )
            cleaned += 1

        except Exception as e:
            error = str(e)
            logger.error(f'  Error processing {p["product_handle"]}: {e}')
            errors += 1

        if not dry_run:
            log_result(
                conn,
                p['product_id'], p['product_handle'], p['product_title'],
                p['age_days'], action_collection, action_tag, error
            )

    conn.close()
    logger.info(f'Complete — cleaned: {cleaned}, errors: {errors}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without making any')
    args = parser.parse_args()
    main(dry_run=args.dry_run)
