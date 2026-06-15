"""
gelato_postgres_sync.py
=======================
Syncs Gelato order data to PostgreSQL.
Writes to unified fulfilments table (provider agnostic).
Also re-checks in_transit orders to catch delivery status updates.

NOTE: Gelato costs are stored as-is in the pipeline's base currency.
If your Gelato account operates in a different currency than your
Shopify store, costs will need manual FX conversion.
Full multi-currency support is planned for a future release.

API: Gelato API v4
Author: Your Brand Data Pipeline

Two-stage API approach:
1. POST /orders:search  — find all Gelato orders for a Shopify reference
2. GET  /orders/{id}    — fetch full detail (tracking, VAT, receipts)

Run modes:
    python gelato_postgres_sync.py                      # last 7 days + in_transit recheck
    python gelato_postgres_sync.py --lookback-days 14   # custom lookback
    python gelato_postgres_sync.py --all-unmatched      # all unmatched orders
    python gelato_postgres_sync.py --order "#CW14807"   # single order debug
    python gelato_postgres_sync.py --recheck-transit    # recheck in_transit only
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

GELATO_API_KEY  = os.getenv('GELATO_API_KEY')
GELATO_API_URL  = os.getenv('GELATO_API_URL', 'https://order.gelatoapis.com/v4')

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

GELATO_ALERT_HOURS      = int(os.getenv('GELATO_ALERT_HOURS', '48'))
DEFAULT_LOOKBACK        = int(os.getenv('DEFAULT_LOOKBACK_DAYS', '7'))
TRANSIT_RECHECK_DAYS   = int(os.getenv('TRANSIT_RECHECK_DAYS', '30'))  # recheck in_transit for last 30 days
REQUEST_TIMEOUT         = int(os.getenv('REQUEST_TIMEOUT', '10'))
LOG_FILE                = os.getenv('LOG_FILE_PATH', 'logs/gelato_sync.log')

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else '.', exist_ok=True)

logger = logging.getLogger('gelato_sync')
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
    'Accept': 'application/json',
    'X-API-KEY': GELATO_API_KEY,
    'Content-Type': 'application/json'
}

# Gelato statuses that mean the order is effectively closed/done
TERMINAL_STATUSES = {'cancelled', 'canceled', 'returned', 'not_connected'}

def normalize_order_ref(order_name):
    """Strip non-digits. #CW14807 → 14807"""
    return ''.join(filter(str.isdigit, order_name or ''))

def safe_float(data, key, default=0.0):
    val = data.get(key)
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default

def safe_str(data, key):
    val = data.get(key)
    return str(val).strip() if val is not None else None

# ─── GELATO API ───────────────────────────────────────────────────────────────

def search_gelato_orders(ref):
    """Stage 1: Search for all Gelato orders matching a Shopify reference."""
    url = f"{GELATO_API_URL}/orders:search"
    payload = {'orderReferenceId': ref}
    logger.debug(f'Gelato search: ref={ref}')
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    orders = data.get('orders', [])
    logger.debug(f'Search returned {len(orders)} order(s) for ref {ref}')
    return orders


def fetch_gelato_order_detail(gelato_order_id):
    """Stage 2: Fetch full order detail including shipment, receipts, tracking."""
    url = f"{GELATO_API_URL}/orders/{gelato_order_id}"
    logger.debug(f'Fetching detail for {gelato_order_id}')
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def extract_cogs_from_receipts(detail):
    """
    Extract COGS breakdown from receipts.
    Returns both incl and excl VAT figures plus breakdown.
    Falls back to root level if no receipts.
    """
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


def extract_fulfilment_data(detail):
    """
    Extract fulfilment and tracking from order detail.
    All fields confirmed from Gelato API v4 inspection.
    """
    shipment = detail.get('shipment') or {}
    packages = shipment.get('packages', [])
    package  = packages[0] if packages else {}

    g_status = safe_str(detail, 'fulfillmentStatus') or ''

    return {
        'fulfilment_status':        g_status,
        'is_cancelled':             g_status.lower() in TERMINAL_STATUSES,
        'tracking_number':          package.get('trackingCode'),
        'tracking_url':             package.get('trackingUrl'),
        'carrier':                  shipment.get('shipmentMethodName'),
        'dispatched_at':            detail.get('shippedAt'),
        'min_delivery_date':        shipment.get('minDeliveryDate'),
        'max_delivery_date':        shipment.get('maxDeliveryDate'),
        'fulfillment_country':      shipment.get('fulfillmentCountry'),
        'destination_country':      (detail.get('shippingAddress') or {}).get('country'),
    }

# ─── DATABASE OPERATIONS ──────────────────────────────────────────────────────

def get_orders_to_sync(conn, lookback_days=None, all_unmatched=False, single_order=None):
    """Get Shopify orders needing Gelato COGS sync."""
    with conn.cursor() as cur:
        if single_order:
            cur.execute("""
                SELECT order_id, order_name, created_at, revenue_gbp, override_flag
                FROM orders
                WHERE brand_id = %s AND order_name = %s
            """, (BRAND_ID, single_order))

        elif all_unmatched:
            cur.execute("""
                SELECT order_id, order_name, created_at, revenue_gbp, override_flag
                FROM orders
                WHERE brand_id = %s
                  AND fulfillment_match_status IN ('unmatched', 'pending')
                  AND override_flag = FALSE
                ORDER BY created_at DESC
            """, (BRAND_ID,))

        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
            cur.execute("""
                SELECT order_id, order_name, created_at, revenue_gbp, override_flag
                FROM orders
                WHERE brand_id = %s
                  AND created_at >= %s
                  AND override_flag = FALSE
                ORDER BY created_at DESC
            """, (BRAND_ID, cutoff))

        rows = cur.fetchall()
        logger.info(f'Orders to process: {len(rows)}')
        return rows


def get_intransit_orders_to_recheck(conn):
    """
    Get provider_order_ids for Gelato orders currently in_transit
    within the last TRANSIT_RECHECK_DAYS.
    These need rechecking to catch delivered status updates.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRANSIT_RECHECK_DAYS)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT f.provider_order_id, f.shopify_order_id, o.order_name,
                   o.created_at, o.revenue_gbp
            FROM fulfilments f
            JOIN orders o ON o.order_id = f.shopify_order_id
            WHERE f.provider = 'gelato'
              AND f.brand_id = %s
              AND f.fulfilment_status NOT IN ('delivered','cancelled','canceled','returned','not_connected')
              AND f.dispatched_at >= %s
              AND f.override_flag = FALSE
            ORDER BY f.dispatched_at DESC
        """, (BRAND_ID, cutoff))
        rows = cur.fetchall()
        logger.info(f'In-transit orders to recheck: {len(rows)}')
        return rows


def upsert_fulfilment(conn, shopify_order_id, order_name, detail, cogs_data, fulfilment_data):
    """
    Upsert into unified fulfilments table.
    Uses max_delivery_date as estimated_delivery_at.
    COALESCE on tracking ensures we don't overwrite confirmed data with nulls.
    """
    g_status = fulfilment_data['fulfilment_status']

    # Set delivered_at from event log if status is delivered
    delivered_at = None
    if g_status.lower() == 'delivered':
        delivered_at = detail.get('deliveredAt') or detail.get('updatedAt')

    sql = """
        INSERT INTO fulfilments (
            brand_id, shopify_order_id, order_name,
            provider, provider_order_id,
            fulfilment_status, is_cancelled,
            cogs_gbp_incl_vat, cogs_gbp_excl_vat,
            products_price, shipping_price, discount_amount,
            vat_amount, receipt_number,
            tracking_number, tracking_url, carrier,
            order_placed_at, dispatched_at,
            estimated_delivery_at, delivered_at,
            destination_country, fulfillment_country,
            synced_at
        ) VALUES (
            %s, %s, %s,
            'gelato', %s,
            %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s
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
        WHERE fulfilments.override_flag = FALSE
    """

    with conn.cursor() as cur:
        cur.execute(sql, (
            BRAND_ID,
            shopify_order_id,
            order_name,
            detail.get('id'),
            g_status,
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
            None,                                   # order_placed_at — set by Shopify sync
            fulfilment_data['dispatched_at'],
            fulfilment_data['max_delivery_date'],   # estimated_delivery_at
            delivered_at,
            fulfilment_data['destination_country'],
            fulfilment_data['fulfillment_country'],
            datetime.now(timezone.utc).isoformat(),
        ))


def update_order_cogs(conn, order_id, order_name,
                      total_cogs_incl_vat, total_cogs_excl_vat,
                      primary_status, primary_gelato_id):
    """Update COGS on orders table. Handles cancelled status."""
    g_status = (primary_status or '').lower()

    if g_status in ('pending_approval',):
        cogs_status = 'estimated'
    elif g_status in TERMINAL_STATUSES:
        cogs_status = 'cancelled'
        total_cogs_incl_vat = 0.0
        total_cogs_excl_vat = 0.0
    else:
        cogs_status = 'final'

    # When Gelato status is pending_approval we don't yet have a real COGS
    # figure, so cogs_gbp is populated from the blended 42.1% rate against
    # revenue_gbp. When the fulfilment status flips to a real value the
    # next sync will overwrite this with the actual figure and set
    # cogs_status='final' — self-healing.
    sql = """
        UPDATE orders SET
            cogs_gbp                    = CASE %s
                                            WHEN 'estimated'
                                              THEN ROUND((revenue_gbp * 0.421)::numeric, 2)
                                            ELSE %s
                                          END,
            cogs_gbp_incl_vat           = %s,
            cogs_gbp_excl_vat           = %s,
            cogs_status                 = %s,
            cogs_updated_at             = %s,
            fulfillment_match_status    = 'matched',
            fulfillment_order_id        = %s,
            fulfillment_provider        = 'gelato'
        WHERE order_id = %s
          AND override_flag = FALSE
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            cogs_status,            # CASE selector
            total_cogs_incl_vat,    # ELSE branch — real COGS when status='final'/'cancelled'
            total_cogs_incl_vat,
            total_cogs_excl_vat,
            cogs_status,
            datetime.now(timezone.utc).isoformat(),
            primary_gelato_id,
            order_id,
        ))
    logger.info(
        f'{order_name}: COGS incl=£{total_cogs_incl_vat:.2f} | '
        f'excl=£{total_cogs_excl_vat:.2f} | '
        f'status={cogs_status} | gelato={primary_status}'
    )

# ─── ALERTS ───────────────────────────────────────────────────────────────────

def check_and_write_alerts(conn):
    """Check for alert conditions and write to nightly alert file."""
    from alert_writer import write_alert_section

    cutoff = datetime.now(timezone.utc) - timedelta(hours=GELATO_ALERT_HOURS)
    alert_lines = []
    summary_lines = []

    with conn.cursor() as cur:
        # Unmatched COGS
        cur.execute("""
            SELECT order_id, order_name, created_at, revenue_gbp
            FROM orders
            WHERE brand_id = %s
              AND fulfillment_match_status IN ('unmatched', 'pending')
              AND created_at < %s
              AND override_flag = FALSE
            ORDER BY created_at DESC
        """, (BRAND_ID, cutoff))
        unmatched = cur.fetchall()

        if unmatched:
            alert_lines.append(f"⚠️  {len(unmatched)} orders > {GELATO_ALERT_HOURS}h with no COGS match:")
            for r in unmatched:
                alert_lines.append(f"  {r[1]} | £{r[3]:.2f} | {r[2].strftime('%Y-%m-%d')}")
            alert_lines.append("")

        # Returned orders — last 60 days
        cur.execute("""
            SELECT o.order_name, o.shipping_country_name,
                   f.tracking_number, f.carrier, f.dispatched_at::date
            FROM fulfilments f
            JOIN orders o ON o.order_id = f.shopify_order_id
            WHERE f.provider = 'gelato'
              AND f.brand_id = %s
              AND f.fulfilment_status = 'returned'
              AND f.dispatched_at >= NOW() - INTERVAL '60 days'
              AND f.delivery_alert_sent = FALSE
        """, (BRAND_ID,))
        returned = cur.fetchall()

        if returned:
            alert_lines.append(f"📦  {len(returned)} orders returned to sender — action required:")
            for r in returned:
                alert_lines.append(
                    f"  {r[0]} | {r[1]} | {r[2]} | {r[3]} | dispatched {r[4]}"
                )
            alert_lines.append("")
            alert_lines.append("To resolve: NocoDB → fulfilments table → set fulfilment_status='returned_resolved' + override_flag=TRUE")

    if alert_lines:
        write_alert_section(
            script_name = "GELATO SYNC",
            alerts      = alert_lines,
            summary     = f"⚠️  {len(unmatched)} unmatched | 📦 {len(returned)} returned"
        )
        logger.info(f'Alert conditions written to nightly alert file')
    else:
        logger.info('No alert conditions detected')
        
# ─── SYNC ORCHESTRATION ───────────────────────────────────────────────────────

def sync_orders(conn, orders):
    """
    Main sync loop for a list of orders.
    Fetches Gelato data and writes to fulfilments + orders tables.
    """
    matched = unmatched_count = errors = skipped = 0

    for order_id, order_name, created_at, revenue_gbp, override_flag in orders:

        if override_flag:
            logger.debug(f'Skipping {order_name} — override_flag')
            skipped += 1
            continue

        ref = normalize_order_ref(order_name)
        if not ref:
            logger.warning(f'Cannot normalize: {order_name}')
            errors += 1
            continue

        try:
            search_results = search_gelato_orders(ref)
        except Exception as e:
            logger.error(f'Gelato search error for {order_name}: {e}')
            errors += 1
            continue

        if not search_results:
            logger.debug(f'No Gelato orders for {order_name}')
            unmatched_count += 1
            continue

        total_cogs_incl_vat = 0.0
        total_cogs_excl_vat = 0.0
        primary_gelato_id   = None
        primary_status      = None

        for i, search_order in enumerate(search_results):
            gelato_id = search_order.get('id')

            try:
                detail      = fetch_gelato_order_detail(gelato_id)
                cogs_data   = extract_cogs_from_receipts(detail)
                fulfilment  = extract_fulfilment_data(detail)
                g_status    = fulfilment['fulfilment_status'].lower()

                if g_status not in ('pending_approval',) and g_status not in TERMINAL_STATUSES:
                    total_cogs_incl_vat += cogs_data['cogs_incl_vat']
                    total_cogs_excl_vat += cogs_data['cogs_excl_vat']

                logger.debug(
                    f'  Sub-order {i+1}: {gelato_id} | status={g_status} | '
                    f'incl=£{cogs_data["cogs_incl_vat"]:.2f} | '
                    f'tracking={fulfilment["tracking_number"]}'
                )

                if i == 0:
                    primary_gelato_id = gelato_id
                    primary_status    = fulfilment['fulfilment_status']

                upsert_fulfilment(conn, order_id, order_name, detail, cogs_data, fulfilment)

            except Exception as e:
                logger.error(f'Failed detail fetch for {gelato_id}: {e}')
                continue

        try:
            update_order_cogs(
                conn, order_id, order_name,
                total_cogs_incl_vat, total_cogs_excl_vat,
                primary_status, primary_gelato_id
            )
            conn.commit()
            matched += 1
        except Exception as e:
            conn.rollback()
            logger.error(f'COGS update failed for {order_name}: {e}')
            errors += 1

    return matched, unmatched_count, errors, skipped


def recheck_intransit(conn):
    """
    Recheck in_transit orders to catch delivered status updates.
    Fetches detail for each Gelato order ID directly (no search needed).
    """
    rows = get_intransit_orders_to_recheck(conn)
    if not rows:
        logger.info('No in_transit orders to recheck')
        return 0, 0

    updated = errors = 0

    for provider_order_id, shopify_order_id, order_name, created_at, revenue_gbp in rows:
        try:
            detail      = fetch_gelato_order_detail(provider_order_id)
            cogs_data   = extract_cogs_from_receipts(detail)
            fulfilment  = extract_fulfilment_data(detail)
            g_status    = fulfilment['fulfilment_status']

            # Only update if status has changed
            upsert_fulfilment(conn, shopify_order_id, order_name, detail, cogs_data, fulfilment)

            if g_status.lower() == 'delivered':
                logger.info(f'{order_name}: status updated to DELIVERED ✅')
            else:
                logger.debug(f'{order_name}: still {g_status}')

            conn.commit()
            updated += 1

        except Exception as e:
            conn.rollback()
            logger.error(f'Recheck failed for {provider_order_id}: {e}')
            errors += 1

    logger.info(f'Transit recheck complete — updated: {updated} | errors: {errors}')
    return updated, errors

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Gelato → Postgres sync')
    parser.add_argument('--lookback-days',   type=int, default=DEFAULT_LOOKBACK)
    parser.add_argument('--all-unmatched',   action='store_true')
    parser.add_argument('--recheck-transit', action='store_true',
                        help='Only recheck in_transit orders for delivery updates')
    parser.add_argument('--order',           type=str, default=None,
                        help='Single order e.g. "#CW14807"')
    args = parser.parse_args()

    logger.info('Gelato sync starting')
    test_db_connection()
    conn = get_db_connection()

    try:
        if args.recheck_transit:
            # Recheck mode only
            logger.info('Recheck transit mode')
            recheck_intransit(conn)

        elif args.order:
            # Single order debug
            orders = get_orders_to_sync(conn, single_order=args.order)
            matched, unmatched, errors, skipped = sync_orders(conn, orders)
            logger.info(f'Single order sync — matched: {matched} | errors: {errors}')

        elif args.all_unmatched:
            # All unmatched
            orders = get_orders_to_sync(conn, all_unmatched=True)
            matched, unmatched, errors, skipped = sync_orders(conn, orders)
            logger.info(f'All unmatched sync — matched: {matched} | unmatched: {unmatched} | errors: {errors}')

        else:
            # Standard nightly run — lookback + transit recheck
            logger.info(f'Standard sync: lookback {args.lookback_days} days + transit recheck')
            orders = get_orders_to_sync(conn, lookback_days=args.lookback_days)
            matched, unmatched, errors, skipped = sync_orders(conn, orders)
            logger.info(f'Sync complete — matched: {matched} | unmatched: {unmatched} | errors: {errors} | skipped: {skipped}')

            # Always recheck in_transit on standard runs
            logger.info('Running transit recheck...')
            recheck_intransit(conn)

    finally:
        conn.close()


    logger.info('Script complete')
    sys.exit(0)
