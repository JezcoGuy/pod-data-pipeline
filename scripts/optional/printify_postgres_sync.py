"""
printify_postgres_sync.py
=========================
Syncs Printify order data to PostgreSQL.
Writes to unified fulfilments table (provider='printify').
Matches Shopify orders via metadata.shop_order_label.

API: Printify API v1
Author: Your Brand Data Pipeline

Key notes:
- Auth: Bearer token (Personal Access Token)
- Base URL: https://api.printify.com/v1
- Shopify ref: metadata.shop_order_label e.g. '#CW1001'
- COGS in pence — divide by 100 for GBP
- cogs_incl_vat = (product_cost + shipping + tax) / 100
- cogs_excl_vat = (product_cost + shipping) / 100
- Tax stored separately — filter by destination_country for accounting
- Tracking: shipments[0].number, carrier, shipped_at, delivered_at
- No VAT breakdown label from Printify — use destination_country to determine type

Run modes:
    python printify_postgres_sync.py                      # last 7 days
    python printify_postgres_sync.py --all-unmatched      # all unmatched orders
    python printify_postgres_sync.py --order "#CW1001"    # single order debug
    python printify_postgres_sync.py --full-backfill      # all Printify orders
"""

import os
import sys
import json
import logging
import smtplib
import argparse
import requests
import psycopg2
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv()

PRINTIFY_TOKEN      = os.getenv('PRINTIFY_TOKEN')
PRINTIFY_SHOP_ID    = os.getenv('PRINTIFY_SHOP_ID')
PRINTIFY_API_URL    = 'https://api.printify.com/v1'

DB_HOST     = os.getenv('DB_HOST', 'localhost')
DB_PORT     = os.getenv('DB_PORT', '5432')
DB_NAME     = os.getenv('DB_NAME')
DB_USER     = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')

BRAND_ID = os.getenv('BRAND_ID', 'your_brand_id')

SMTP_HOST   = os.getenv('SMTP_HOST', 'smtp.fastmail.com')
SMTP_PORT   = int(os.getenv('SMTP_PORT', '465'))
SMTP_USER   = os.getenv('SMTP_USER')
SMTP_PASS   = os.getenv('SMTP_PASS')
SMTP_FROM   = os.getenv('SMTP_FROM')
SMTP_TO     = os.getenv('SMTP_TO')

DEFAULT_LOOKBACK    = int(os.getenv('DEFAULT_LOOKBACK_DAYS', '7'))
REQUEST_TIMEOUT     = int(os.getenv('REQUEST_TIMEOUT', '30'))
LOG_FILE            = os.getenv('LOG_FILE_PATH', 'logs/printify_sync.log')

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else '.', exist_ok=True)

logger = logging.getLogger('printify_sync')
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
    logger.debug('Testing Postgres connection')
    try:
        conn = get_db_connection()
        conn.close()
        logger.info('Postgres connection OK')
    except Exception as e:
        logger.critical(f'Postgres connection failed: {e}')
        sys.exit(1)

# ─── HELPERS ──────────────────────────────────────────────────────────────────

HEADERS = {
    'Authorization': f'Bearer {PRINTIFY_TOKEN}',
    'Content-Type': 'application/json'
}

# Printify statuses that mean order is effectively closed
TERMINAL_STATUSES = {'canceled', 'cancelled'}

def pence_to_gbp(pence):
    """Convert Printify pence value to GBP."""
    try:
        return round(float(pence) / 100, 4) if pence else 0.0
    except (TypeError, ValueError):
        return 0.0

# ─── PRINTIFY API ─────────────────────────────────────────────────────────────

def fetch_printify_orders_page(page=1, limit=10):
    """Fetch a page of Printify orders."""
    url = f"{PRINTIFY_API_URL}/shops/{PRINTIFY_SHOP_ID}/orders.json"
    params = {'limit': limit, 'page': page}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_all_printify_orders():
    """
    Fetch all Printify orders via pagination.
    Returns generator of order dicts.
    """
    page = 1
    limit = 10
    total_pages = None

    while True:
        data = fetch_printify_orders_page(page=page, limit=limit)
        orders = data.get('data', [])
        total_pages = total_pages or data.get('last_page', 1)

        logger.debug(f'Fetched page {page}/{total_pages} — {len(orders)} orders')

        for order in orders:
            yield order

        if page >= total_pages:
            break
        page += 1


def extract_shopify_ref(order):
    """
    Extract Shopify order reference from Printify metadata.
    Returns order_name e.g. '#CW1001' or None.
    """
    meta = order.get('metadata') or {}
    return meta.get('shop_order_label')  # e.g. '#CW1001'


def extract_cogs(order):
    """
    Extract COGS from Printify order.
    All values in pence — convert to GBP.

    cogs_incl_vat = product + shipping + tax (everything charged)
    cogs_excl_vat = product + shipping only
    tax stored separately for accounting by destination country.

    Note: Printify has no VAT label — use destination_country to
    determine tax type (UK=VAT reclaimable, US=sales tax not reclaimable).
    """
    line_items = order.get('line_items', [])

    # Sum across all line items
    product_cost_pence  = sum(item.get('cost', 0) for item in line_items)
    shipping_cost_pence = sum(item.get('shipping_cost', 0) for item in line_items)
    tax_pence           = order.get('total_tax', 0) or 0

    cogs_excl_vat = pence_to_gbp(product_cost_pence + shipping_cost_pence)
    cogs_incl_vat = pence_to_gbp(product_cost_pence + shipping_cost_pence + tax_pence)
    tax_amount    = pence_to_gbp(tax_pence)

    return {
        'cogs_incl_vat':    cogs_incl_vat,
        'cogs_excl_vat':    cogs_excl_vat,
        'products_price':   pence_to_gbp(product_cost_pence),
        'shipping_price':   pence_to_gbp(shipping_cost_pence),
        'vat_amount':       tax_amount,
        'discount_amount':  None,   # Printify has no loyalty discount
        'receipt_number':   order.get('app_order_id'),
    }


def extract_fulfilment(order):
    """
    Extract fulfilment and tracking from Printify order.
    Uses first shipment record.
    """
    shipments = order.get('shipments', [])
    shipment  = shipments[0] if shipments else {}

    status = order.get('status', '')
    is_cancelled = status.lower() in TERMINAL_STATUSES

    # Get destination country
    address = order.get('address_to') or {}
    destination_country = address.get('country')

    # Map country name to code for consistency
    country_map = {
        'United Kingdom': 'GB',
        'United States': 'US',
        'Germany': 'DE',
        'France': 'FR',
        'Netherlands': 'NL',
        'Australia': 'AU',
        'Canada': 'CA',
        'Sweden': 'SE',
        'Norway': 'NO',
        'Denmark': 'DK',
        'Ireland': 'IE',
        'Belgium': 'BE',
        'Spain': 'ES',
        'Italy': 'IT',
        'Poland': 'PL',
        'Finland': 'FI',
        'Portugal': 'PT',
        'Austria': 'AT',
        'Switzerland': 'CH',
        'New Zealand': 'NZ',
        'Japan': 'JP',
        'Singapore': 'SG',
        'Hong Kong': 'HK',
        'Mexico': 'MX',
    }
    country_code = country_map.get(destination_country, destination_country)

    return {
        'fulfilment_status':    status,
        'is_cancelled':         is_cancelled,
        'tracking_number':      shipment.get('number'),
        'tracking_url':         shipment.get('url'),
        'carrier':              shipment.get('carrier'),
        'dispatched_at':        shipment.get('shipped_at'),
        'delivered_at':         shipment.get('delivered_at'),
        'estimated_delivery_at': (order.get('line_items') or [{}])[0].get('estimated_delivery_at'),
        'destination_country':  country_code,
    }

# ─── DATABASE OPERATIONS ──────────────────────────────────────────────────────

def get_unmatched_orders(conn):
    """Get all Shopify orders currently unmatched (Printify era)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT order_id, order_name, created_at, revenue_gbp
            FROM orders
            WHERE brand_id = %s
              AND fulfillment_match_status = 'unmatched'
              AND override_flag = FALSE
            ORDER BY created_at ASC
        """, (BRAND_ID,))
        rows = cur.fetchall()
    logger.info(f'Unmatched orders in database: {len(rows)}')
    return {r[1]: r for r in rows}  # keyed by order_name e.g. '#CW1001'


def get_recent_orders(conn, lookback_days):
    """Get recent Shopify orders for incremental sync."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT order_id, order_name, created_at, revenue_gbp
            FROM orders
            WHERE brand_id = %s
              AND created_at >= %s
              AND override_flag = FALSE
            ORDER BY created_at DESC
        """, (BRAND_ID, cutoff))
        rows = cur.fetchall()
    logger.info(f'Recent orders to check: {len(rows)}')
    return {r[1]: r for r in rows}


def upsert_fulfilment(conn, shopify_order_id, order_name,
                      printify_order, cogs_data, fulfilment_data):
    """Upsert Printify order into unified fulfilments table."""

    delivered_at = fulfilment_data.get('delivered_at')

    sql = """
        INSERT INTO fulfilments (
            brand_id, shopify_order_id, order_name,
            provider, provider_order_id,
            fulfilment_status, is_cancelled,
            cogs_gbp_incl_vat, cogs_gbp_excl_vat,
            products_price, shipping_price,
            discount_amount, vat_amount, receipt_number,
            tracking_number, tracking_url, carrier,
            order_placed_at, dispatched_at,
            estimated_delivery_at, delivered_at,
            destination_country,
            synced_at
        ) VALUES (
            %s, %s, %s,
            'printify', %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s,
            %s
        )
        ON CONFLICT (provider, provider_order_id) DO UPDATE SET
            fulfilment_status       = EXCLUDED.fulfilment_status,
            is_cancelled            = EXCLUDED.is_cancelled,
            cogs_gbp_incl_vat       = EXCLUDED.cogs_gbp_incl_vat,
            cogs_gbp_excl_vat       = EXCLUDED.cogs_gbp_excl_vat,
            products_price          = EXCLUDED.products_price,
            shipping_price          = EXCLUDED.shipping_price,
            vat_amount              = EXCLUDED.vat_amount,
            receipt_number          = COALESCE(EXCLUDED.receipt_number, fulfilments.receipt_number),
            tracking_number         = COALESCE(EXCLUDED.tracking_number, fulfilments.tracking_number),
            tracking_url            = COALESCE(EXCLUDED.tracking_url, fulfilments.tracking_url),
            carrier                 = COALESCE(EXCLUDED.carrier, fulfilments.carrier),
            dispatched_at           = COALESCE(EXCLUDED.dispatched_at, fulfilments.dispatched_at),
            estimated_delivery_at   = COALESCE(EXCLUDED.estimated_delivery_at, fulfilments.estimated_delivery_at),
            delivered_at            = COALESCE(EXCLUDED.delivered_at, fulfilments.delivered_at),
            destination_country     = COALESCE(EXCLUDED.destination_country, fulfilments.destination_country),
            synced_at               = EXCLUDED.synced_at
        WHERE fulfilments.override_flag = FALSE
    """

    with conn.cursor() as cur:
        cur.execute(sql, (
            BRAND_ID,
            shopify_order_id,
            order_name,
            printify_order['id'],
            fulfilment_data['fulfilment_status'],
            fulfilment_data['is_cancelled'],
            cogs_data['cogs_incl_vat'],
            cogs_data['cogs_excl_vat'],
            cogs_data['products_price'],
            cogs_data['shipping_price'],
            cogs_data['discount_amount'],
            cogs_data['vat_amount'],
            cogs_data['receipt_number'],
            fulfilment_data['tracking_number'],
            fulfilment_data['tracking_url'],
            fulfilment_data['carrier'],
            printify_order.get('created_at'),
            fulfilment_data['dispatched_at'],
            fulfilment_data['estimated_delivery_at'],
            delivered_at,
            fulfilment_data['destination_country'],
            datetime.now(timezone.utc).isoformat(),
        ))


def update_order_cogs(conn, order_id, order_name,
                      cogs_incl_vat, cogs_excl_vat,
                      printify_status, printify_order_id):
    """Update COGS on orders table for matched Printify order."""

    status_lower = (printify_status or '').lower()
    if status_lower in TERMINAL_STATUSES:
        cogs_status = 'cancelled'
        cogs_incl_vat = 0.0
        cogs_excl_vat = 0.0
    else:
        cogs_status = 'final'

    sql = """
        UPDATE orders SET
            cogs_gbp                    = %s,
            cogs_gbp_incl_vat           = %s,
            cogs_gbp_excl_vat           = %s,
            cogs_status                 = %s,
            cogs_updated_at             = %s,
            fulfillment_match_status    = 'matched',
            fulfillment_order_id        = %s,
            fulfillment_provider        = 'printify'
        WHERE order_id = %s
          AND override_flag = FALSE
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            cogs_incl_vat,
            cogs_incl_vat,
            cogs_excl_vat,
            cogs_status,
            datetime.now(timezone.utc).isoformat(),
            printify_order_id,
            order_id,
        ))

    logger.info(
        f'{order_name}: COGS incl=£{cogs_incl_vat:.2f} | '
        f'excl=£{cogs_excl_vat:.2f} | '
        f'status={cogs_status} | printify={printify_status}'
    )

# ─── ALERTS ───────────────────────────────────────────────────────────────────


# ─── MAIN SYNC ────────────────────────────────────────────────────────────────

def run_sync(all_unmatched=False, lookback_days=None,
             single_order=None, full_backfill=False):
    """
    Main sync logic.

    Fetches Printify orders and matches to Shopify orders in database.
    Matching: metadata.shop_order_label → orders.order_name

    Modes:
    - single_order: debug one order
    - all_unmatched: process all unmatched Shopify orders
    - full_backfill: fetch all Printify orders regardless
    - default: fetch recent Printify orders (last 7 days)
    """
    test_db_connection()
    conn = get_db_connection()

    matched = unmatched = errors = skipped = no_shopify_ref = 0

    try:
        # Get our Shopify orders to match against
        if single_order:
            shopify_orders = get_unmatched_orders(conn)
            shopify_orders.update(get_recent_orders(conn, 30))
        elif all_unmatched or full_backfill:
            shopify_orders = get_unmatched_orders(conn)
        else:
            shopify_orders = get_recent_orders(conn, lookback_days)

        logger.info(f'Shopify orders to match: {len(shopify_orders)}')

        # Fetch Printify orders
        if single_order:
            # Find the specific Printify order by searching
            logger.info(f'Looking for Printify order matching {single_order}')
            printify_orders_to_process = []
            for p_order in fetch_all_printify_orders():
                ref = extract_shopify_ref(p_order)
                if ref == single_order:
                    printify_orders_to_process.append(p_order)
                    break
        else:
            printify_orders_to_process = list(fetch_all_printify_orders())

        logger.info(f'Printify orders fetched: {len(printify_orders_to_process)}')

        for p_order in printify_orders_to_process:
            shopify_ref = extract_shopify_ref(p_order)

            if not shopify_ref:
                logger.debug(f'No Shopify ref for Printify order {p_order["id"]} — skipping')
                no_shopify_ref += 1
                continue

            if shopify_ref not in shopify_orders:
                logger.debug(f'{shopify_ref} not in our orders — skipping')
                skipped += 1
                continue

            order_id, order_name, created_at, revenue_gbp = shopify_orders[shopify_ref]

            try:
                cogs_data       = extract_cogs(p_order)
                fulfilment_data = extract_fulfilment(p_order)

                upsert_fulfilment(
                    conn, order_id, order_name,
                    p_order, cogs_data, fulfilment_data
                )

                update_order_cogs(
                    conn, order_id, order_name,
                    cogs_data['cogs_incl_vat'],
                    cogs_data['cogs_excl_vat'],
                    p_order.get('status'),
                    p_order['id']
                )

                conn.commit()
                matched += 1

            except Exception as e:
                conn.rollback()
                logger.error(f'Failed for {shopify_ref}: {e}')
                errors += 1

    finally:
        conn.close()

    logger.info(
        f'Printify sync complete — '
        f'matched: {matched} | unmatched: {unmatched} | '
        f'errors: {errors} | skipped: {skipped} | '
        f'no_shopify_ref: {no_shopify_ref}'
    )

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Printify → Postgres sync')
    parser.add_argument('--lookback-days',  type=int, default=DEFAULT_LOOKBACK)
    parser.add_argument('--all-unmatched',  action='store_true',
                        help='Match all unmatched Shopify orders')
    parser.add_argument('--full-backfill',  action='store_true',
                        help='Fetch all Printify orders regardless of match status')
    parser.add_argument('--order',          type=str, default=None,
                        help='Single order e.g. "#CW1001"')
    args = parser.parse_args()

    logger.info('Printify sync starting')

    run_sync(
        all_unmatched=args.all_unmatched,
        lookback_days=args.lookback_days if not args.all_unmatched and not args.order and not args.full_backfill else None,
        single_order=args.order,
        full_backfill=args.full_backfill,
    )

    logger.info('Script complete')
    sys.exit(0)
