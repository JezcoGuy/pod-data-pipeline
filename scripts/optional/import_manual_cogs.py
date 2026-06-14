"""
import_manual_cogs.py
====================
Resolves unmatched orders by fetching full data from Gelato or Printify API.
Updates fulfilments + orders tables exactly like the nightly scripts.

Usage:
    python3 import_manual_cogs.py --file unmatched_orders_template.csv
    python3 import_manual_cogs.py --file unmatched_orders_template.csv --dry-run

CSV format (3 columns):
    order_name, fulfillment_provider, fulfillment_order_id, notes

    order_name:           e.g. #CW1112
    fulfillment_provider: gelato / printify / manual
    fulfillment_order_id: Gelato UUID or Printify order ID
                          Leave blank for cancelled/unresolvable orders
    notes:                optional — for your reference only

Examples:
    #CW1112, gelato, abc-123-uuid-def, found in Gelato dashboard
    #CW1116, printify, 678104d6993f43852206438d,
    #CW1152, manual, , cancelled before fulfilment

For gelato/printify rows with an order ID:
    - Script calls the API and fetches full COGS, tracking, VAT, delivery data
    - Writes to fulfilments table (same as nightly script)
    - Updates orders table with COGS and match status

For manual rows or blank order IDs:
    - Sets fulfillment_match_status = manual
    - Sets cogs_gbp = 0, cogs_status = manual_override
    - Sets override_flag = TRUE
"""

import os
import sys
import csv
import logging
import argparse
import requests
import psycopg2
from datetime import datetime, timezone
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv()

GELATO_API_KEY      = os.getenv('GELATO_API_KEY')
GELATO_API_URL      = os.getenv('GELATO_API_URL', 'https://order.gelatoapis.com/v4')
PRINTIFY_TOKEN      = os.getenv('PRINTIFY_TOKEN')
PRINTIFY_SHOP_ID    = os.getenv('PRINTIFY_SHOP_ID')
PRINTIFY_API_URL    = 'https://api.printify.com/v1'

DB_HOST     = os.getenv('DB_HOST', 'localhost')
DB_PORT     = os.getenv('DB_PORT', '5432')
DB_NAME     = os.getenv('DB_NAME')
DB_USER     = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
BRAND_ID    = os.getenv('BRAND_ID', 'your_brand_id')

REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '30'))

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger('import_manual_cogs')

# ─── DATABASE ─────────────────────────────────────────────────────────────────

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD
    )

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def safe_float(data, key, default=0.0):
    val = data.get(key)
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default

def safe_str(data, key):
    val = data.get(key)
    return str(val).strip() if val is not None else None

def pence_to_gbp(pence):
    try:
        return round(float(pence) / 100, 4) if pence else 0.0
    except (TypeError, ValueError):
        return 0.0

# ─── GELATO API ───────────────────────────────────────────────────────────────

GELATO_HEADERS = {
    'Accept': 'application/json',
    'X-API-KEY': GELATO_API_KEY,
    'Content-Type': 'application/json'
}

def fetch_gelato_detail(gelato_order_id):
    """Fetch full Gelato order detail by ID."""
    url = f"{GELATO_API_URL}/orders/{gelato_order_id}"
    logger.debug(f'Fetching Gelato order {gelato_order_id}')
    resp = requests.get(url, headers=GELATO_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def extract_gelato_cogs(detail):
    """Extract COGS from Gelato receipts. Same logic as gelato_postgres_sync.py"""
    receipts = detail.get('receipts', [])
    if not receipts:
        return {
            'cogs_incl_vat':    safe_float(detail, 'totalInclVat'),
            'cogs_excl_vat':    safe_float(detail, 'total'),
            'products_price':   None,
            'shipping_price':   None,
            'discount_amount':  None,
            'vat_amount':       None,
            'receipt_number':   None,
        }
    r = receipts[0]
    return {
        'cogs_incl_vat':    safe_float(r, 'totalInclVat'),
        'cogs_excl_vat':    safe_float(r, 'total'),
        'products_price':   safe_float(r, 'productsPrice'),
        'shipping_price':   safe_float(r, 'shippingPrice'),
        'discount_amount':  safe_float(r, 'discount'),
        'vat_amount':       safe_float(r, 'totalVat'),
        'receipt_number':   safe_str(r, 'receiptNumber'),
    }

def extract_gelato_fulfilment(detail):
    """Extract fulfilment data from Gelato order. Same logic as gelato_postgres_sync.py"""
    shipment = detail.get('shipment') or {}
    packages = shipment.get('packages', [])
    package  = packages[0] if packages else {}
    g_status = safe_str(detail, 'fulfillmentStatus') or ''

    delivered_at = None
    if g_status.lower() == 'delivered':
        delivered_at = detail.get('deliveredAt') or detail.get('updatedAt')

    return {
        'fulfilment_status':        g_status,
        'is_cancelled':             g_status.lower() in ('cancelled','canceled','returned','not_connected'),
        'tracking_number':          package.get('trackingCode'),
        'tracking_url':             package.get('trackingUrl'),
        'carrier':                  shipment.get('shipmentMethodName'),
        'dispatched_at':            detail.get('shippedAt'),
        'min_delivery_date':        shipment.get('minDeliveryDate'),
        'max_delivery_date':        shipment.get('maxDeliveryDate'),
        'fulfillment_country':      shipment.get('fulfillmentCountry'),
        'destination_country':      (detail.get('shippingAddress') or {}).get('country'),
        'delivered_at':             delivered_at,
    }

# ─── PRINTIFY API ─────────────────────────────────────────────────────────────

PRINTIFY_HEADERS = {
    'Authorization': f'Bearer {PRINTIFY_TOKEN}',
    'Content-Type': 'application/json'
}

def fetch_printify_detail(printify_order_id):
    """Fetch full Printify order detail by ID."""
    url = f"{PRINTIFY_API_URL}/shops/{PRINTIFY_SHOP_ID}/orders/{printify_order_id}.json"
    logger.debug(f'Fetching Printify order {printify_order_id}')
    resp = requests.get(url, headers=PRINTIFY_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()

def extract_printify_cogs(order):
    """Extract COGS from Printify order. Same logic as printify_postgres_sync.py"""
    line_items = order.get('line_items', [])
    product_cost_pence  = sum(item.get('cost', 0) for item in line_items)
    shipping_cost_pence = sum(item.get('shipping_cost', 0) for item in line_items)
    tax_pence           = order.get('total_tax', 0) or 0

    return {
        'cogs_incl_vat':    pence_to_gbp(product_cost_pence + shipping_cost_pence + tax_pence),
        'cogs_excl_vat':    pence_to_gbp(product_cost_pence + shipping_cost_pence),
        'products_price':   pence_to_gbp(product_cost_pence),
        'shipping_price':   pence_to_gbp(shipping_cost_pence),
        'vat_amount':       pence_to_gbp(tax_pence),
        'discount_amount':  None,
        'receipt_number':   order.get('app_order_id'),
    }

def extract_printify_fulfilment(order):
    """Extract fulfilment from Printify order. Same logic as printify_postgres_sync.py"""
    shipments = order.get('shipments', [])
    shipment  = shipments[0] if shipments else {}
    status    = order.get('status', '')
    address   = order.get('address_to') or {}

    country_map = {
        'United Kingdom': 'GB', 'United States': 'US', 'Germany': 'DE',
        'France': 'FR', 'Netherlands': 'NL', 'Australia': 'AU',
        'Canada': 'CA', 'Sweden': 'SE', 'Norway': 'NO', 'Denmark': 'DK',
        'Ireland': 'IE', 'Belgium': 'BE', 'Spain': 'ES', 'Italy': 'IT',
        'Poland': 'PL', 'Finland': 'FI', 'Portugal': 'PT', 'Austria': 'AT',
        'Switzerland': 'CH', 'New Zealand': 'NZ', 'Japan': 'JP',
        'Singapore': 'SG', 'Hong Kong': 'HK', 'Mexico': 'MX',
    }
    destination_country = address.get('country')
    country_code = country_map.get(destination_country, destination_country)

    line_items = order.get('line_items', [])
    estimated_delivery = (line_items[0].get('estimated_delivery_at') if line_items else None)

    return {
        'fulfilment_status':        status,
        'is_cancelled':             status.lower() in ('canceled', 'cancelled'),
        'tracking_number':          shipment.get('number'),
        'tracking_url':             shipment.get('url'),
        'carrier':                  shipment.get('carrier'),
        'dispatched_at':            shipment.get('shipped_at'),
        'delivered_at':             shipment.get('delivered_at'),
        'estimated_delivery_at':    estimated_delivery,
        'destination_country':      country_code,
        'fulfillment_country':      None,
    }

# ─── DATABASE OPERATIONS ──────────────────────────────────────────────────────

def get_order(conn, order_name):
    """Look up order in database."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT order_id, created_at, revenue_gbp, fulfillment_match_status
            FROM orders
            WHERE brand_id = %s AND order_name = %s
        """, (BRAND_ID, order_name))
        return cur.fetchone()


def upsert_fulfilment_record(conn, order_id, order_name, provider,
                              provider_order_id, cogs_data, fulfilment_data):
    """Upsert into fulfilments table."""
    delivered_at = fulfilment_data.get('delivered_at')
    estimated    = fulfilment_data.get('estimated_delivery_at') or fulfilment_data.get('max_delivery_date')

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fulfilments (
                brand_id, shopify_order_id, order_name,
                provider, provider_order_id,
                fulfilment_status, is_cancelled,
                cogs_gbp_incl_vat, cogs_gbp_excl_vat,
                products_price, shipping_price,
                discount_amount, vat_amount, receipt_number,
                tracking_number, tracking_url, carrier,
                dispatched_at, estimated_delivery_at, delivered_at,
                destination_country, fulfillment_country,
                override_flag, synced_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s,
                TRUE, %s
            )
            ON CONFLICT (provider, provider_order_id) DO UPDATE SET
                fulfilment_status       = EXCLUDED.fulfilment_status,
                is_cancelled            = EXCLUDED.is_cancelled,
                cogs_gbp_incl_vat       = EXCLUDED.cogs_gbp_incl_vat,
                cogs_gbp_excl_vat       = EXCLUDED.cogs_gbp_excl_vat,
                products_price          = EXCLUDED.products_price,
                shipping_price          = EXCLUDED.shipping_price,
                discount_amount         = EXCLUDED.discount_amount,
                vat_amount              = EXCLUDED.vat_amount,
                receipt_number          = COALESCE(EXCLUDED.receipt_number, fulfilments.receipt_number),
                tracking_number         = COALESCE(EXCLUDED.tracking_number, fulfilments.tracking_number),
                tracking_url            = COALESCE(EXCLUDED.tracking_url, fulfilments.tracking_url),
                carrier                 = COALESCE(EXCLUDED.carrier, fulfilments.carrier),
                dispatched_at           = COALESCE(EXCLUDED.dispatched_at, fulfilments.dispatched_at),
                estimated_delivery_at   = COALESCE(EXCLUDED.estimated_delivery_at, fulfilments.estimated_delivery_at),
                delivered_at            = COALESCE(EXCLUDED.delivered_at, fulfilments.delivered_at),
                destination_country     = COALESCE(EXCLUDED.destination_country, fulfilments.destination_country),
                fulfillment_country     = COALESCE(EXCLUDED.fulfillment_country, fulfilments.fulfillment_country),
                synced_at               = EXCLUDED.synced_at
        """, (
            BRAND_ID, order_id, order_name,
            provider, provider_order_id,
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
            fulfilment_data['dispatched_at'],
            estimated,
            delivered_at,
            fulfilment_data['destination_country'],
            fulfilment_data.get('fulfillment_country'),
            datetime.now(timezone.utc).isoformat(),
        ))


def update_order_cogs(conn, order_id, order_name, provider,
                      provider_order_id, cogs_data, fulfilment_data):
    """Update orders table with COGS and match status."""
    g_status = (fulfilment_data['fulfilment_status'] or '').lower()

    if g_status in ('pending_approval',):
        cogs_status = 'estimated'
    elif fulfilment_data['is_cancelled']:
        cogs_status = 'cancelled'
        cogs_data['cogs_incl_vat'] = 0.0
        cogs_data['cogs_excl_vat'] = 0.0
    else:
        cogs_status = 'final'

    # See gelato_postgres_sync.update_order_cogs — same self-healing trick
    # for the 'estimated' branch: cogs_gbp is computed from the blended
    # 42.1% of revenue_gbp until the fulfilment finalises.
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE orders SET
                cogs_gbp                 = CASE %s
                                            WHEN 'estimated'
                                              THEN ROUND((revenue_gbp * 0.421)::numeric, 2)
                                            ELSE %s
                                          END,
                cogs_gbp_incl_vat        = %s,
                cogs_gbp_excl_vat        = %s,
                cogs_status              = %s,
                cogs_updated_at          = %s,
                fulfillment_match_status = 'manual',
                fulfillment_provider     = %s,
                fulfillment_order_id     = %s,
                override_flag            = TRUE
            WHERE order_id = %s
        """, (
            cogs_status,                       # CASE selector
            cogs_data['cogs_incl_vat'],        # ELSE branch
            cogs_data['cogs_incl_vat'],
            cogs_data['cogs_excl_vat'],
            cogs_status,
            datetime.now(timezone.utc).isoformat(),
            provider,
            provider_order_id,
            order_id,
        ))

    logger.info(
        f'{order_name}: updated \u2014 '
        f'incl=\u00a3{cogs_data["cogs_incl_vat"]:.2f} | '
        f'excl=\u00a3{cogs_data["cogs_excl_vat"]:.2f} | '
        f'provider={provider} | status={cogs_status}'
    )


def mark_manual(conn, order_id, order_name, notes=None):
    """Mark order as manually reviewed with no COGS data available."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE orders SET
                cogs_gbp                 = 0,
                cogs_gbp_incl_vat        = 0,
                cogs_gbp_excl_vat        = 0,
                cogs_status              = 'manual_override',
                cogs_updated_at          = %s,
                fulfillment_match_status = 'manual',
                override_flag            = TRUE
            WHERE order_id = %s
        """, (datetime.now(timezone.utc).isoformat(), order_id))
    logger.info(f'{order_name}: marked as manual (no COGS data) — {notes or ""}')

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def read_csv(filepath):
    rows = []
    with open(filepath, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            order_name = row.get('order_name', '').strip()
            if not order_name:
                continue
            provider = row.get('fulfillment_provider', '').strip().lower() or 'manual'
            if provider not in ('gelato', 'printify', 'manual', 'other'):
                logger.warning(f'Row {i}: invalid provider "{provider}" — defaulting to manual')
                provider = 'manual'
            rows.append({
                'order_name':           order_name,
                'fulfillment_provider': provider,
                'fulfillment_order_id': row.get('fulfillment_order_id', '').strip() or None,
                'notes':                row.get('notes', '').strip() or None,
            })
    logger.info(f'CSV loaded: {len(rows)} rows')
    return rows


def process_row(conn, row, dry_run=False):
    """Process a single CSV row."""
    order_name   = row['order_name']
    provider     = row['fulfillment_provider']
    order_id_ref = row['fulfillment_order_id']
    notes        = row['notes']

    # Look up order in database
    result = get_order(conn, order_name)
    if not result:
        logger.warning(f'{order_name}: not found in database — skipping')
        return False

    order_id = result[0]

    # No provider order ID — mark as manual
    if not order_id_ref or provider == 'manual':
        if dry_run:
            logger.info(f'[DRY RUN] {order_name}: mark as manual (no order ID) — {notes or ""}')
            return True
        mark_manual(conn, order_id, order_name, notes)
        return True

    # Fetch from API
    try:
        if provider == 'gelato':
            detail      = fetch_gelato_detail(order_id_ref)
            cogs_data   = extract_gelato_cogs(detail)
            fulfilment  = extract_gelato_fulfilment(detail)

        elif provider == 'printify':
            detail      = fetch_printify_detail(order_id_ref)
            cogs_data   = extract_printify_cogs(detail)
            fulfilment  = extract_printify_fulfilment(detail)

        else:
            logger.warning(f'{order_name}: unknown provider {provider} — marking manual')
            if not dry_run:
                mark_manual(conn, order_id, order_name, notes)
            return True

    except Exception as e:
        logger.error(f'{order_name}: API error — {e}')
        return False

    if dry_run:
        logger.info(
            f'[DRY RUN] {order_name}: '
            f'provider={provider} | '
            f'incl=\u00a3{cogs_data["cogs_incl_vat"]:.2f} | '
            f'excl=\u00a3{cogs_data["cogs_excl_vat"]:.2f} | '
            f'status={fulfilment["fulfilment_status"]} | '
            f'tracking={fulfilment["tracking_number"]}'
        )
        return True

    # Write to database
    upsert_fulfilment_record(
        conn, order_id, order_name, provider,
        order_id_ref, cogs_data, fulfilment
    )
    update_order_cogs(
        conn, order_id, order_name, provider,
        order_id_ref, cogs_data, fulfilment
    )
    return True


def run_import(filepath, dry_run=False):
    if not os.path.exists(filepath):
        logger.error(f'File not found: {filepath}')
        sys.exit(1)

    rows = read_csv(filepath)
    if not rows:
        logger.error('No valid rows in CSV')
        sys.exit(1)

    if dry_run:
        logger.info('=== DRY RUN \u2014 no changes will be made ===')

    conn = get_db_connection()
    conn.autocommit = False
    updated = skipped = errors = 0

    try:
        for row in rows:
            try:
                if process_row(conn, row, dry_run=dry_run):
                    updated += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f'{row["order_name"]}: {e}')
                conn.rollback()
                errors += 1

        if not dry_run:
            conn.commit()
            logger.info('All changes committed')

    except Exception as e:
        conn.rollback()
        logger.error(f'Import failed: {e}')
        sys.exit(1)
    finally:
        conn.close()

    logger.info(f'Done \u2014 updated: {updated} | skipped: {skipped} | errors: {errors}')
    if dry_run:
        logger.info('Run without --dry-run to apply changes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Resolve unmatched orders via API lookup')
    parser.add_argument('--file',    required=True, help='Path to CSV file')
    parser.add_argument('--dry-run', action='store_true', help='Preview without updating')
    args = parser.parse_args()
    run_import(args.file, dry_run=args.dry_run)
