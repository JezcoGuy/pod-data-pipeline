"""
shopify_postgres_sync.py
========================
Syncs Shopify orders, line items and customers to PostgreSQL.
Pure data ingestion — no alert logic.

API version: 2025-04
Uses updated_at_min for incremental sync (catches status changes on old orders).

Usage:
    python shopify_postgres_sync.py                        # last 3 days
    python shopify_postgres_sync.py --lookback-days 7      # custom lookback
    python shopify_postgres_sync.py --lookback-days 500    # full backfill
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
import requests
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv("/opt/your_brand_id/.env")

SHOPIFY_ACCESS_TOKEN    = os.getenv('SHOPIFY_ACCESS_TOKEN')
SHOPIFY_STORE           = os.getenv('SHOPIFY_STORE_NAME')
SHOPIFY_PLAN_RATE       = float(os.getenv('SHOPIFY_PLAN_RATE', '0.017'))
SHOPIFY_PLAN_FIXED_FEE  = float(os.getenv('SHOPIFY_PLAN_FIXED_FEE', '0.25'))

DB_HOST     = os.getenv('DB_HOST', 'localhost')
DB_PORT     = os.getenv('DB_PORT', '5432')
DB_NAME     = os.getenv('DB_NAME')
DB_USER     = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')

BRAND_ID        = os.getenv('BRAND_ID', 'your_brand_id')
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '30'))
DEFAULT_LOOKBACK = int(os.getenv('DEFAULT_LOOKBACK_DAYS', '3'))
LOG_FILE        = os.getenv('LOG_FILE_PATH', 'logs/shopify_sync.log')

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else '.', exist_ok=True)

logger = logging.getLogger('shopify_sync')
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

BASE_URL        = f"https://{SHOPIFY_STORE}.myshopify.com/admin/api/2025-04"
ORDERS_ENDPOINT = f"{BASE_URL}/orders.json"
HEADERS         = {'X-Shopify-Access-Token': SHOPIFY_ACCESS_TOKEN}


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
        except requests.exceptions.ConnectionError as e:
            logger.warning(f'Connection error — retrying in {backoff}s: {e}')
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    raise Exception(f'Failed after {max_retries} attempts: {url}')


def fetch_orders(updated_at_min, updated_at_max=None):
    """
    Fetch all orders updated since updated_at_min.
    Uses updated_at_min so status changes on old orders are captured.
    Paginates automatically.
    """
    logger.info(f'Fetching orders updated since {updated_at_min}')
    params = {
        'status':         'any',
        'limit':          250,
        'updated_at_min': updated_at_min,
    }
    if updated_at_max:
        params['updated_at_max'] = updated_at_max

    orders = []
    url    = ORDERS_ENDPOINT

    while url:
        resp  = request_with_retry(url, params=params)
        batch = resp.json().get('orders', [])
        logger.debug(f'Batch: {len(batch)} orders')
        orders.extend(batch)

        link = resp.headers.get('Link', '')
        if 'rel="next"' not in link:
            break
        parts = [p.split(';') for p in link.split(',')]
        url   = next(
            (p[0].strip('<> ') for p in parts if len(p) > 1 and 'rel="next"' in p[1]),
            None
        )
        params = None  # params embedded in next URL

    logger.info(f'Total orders fetched: {len(orders)}')
    return orders

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def parse_utms(landing_site):
    if not landing_site:
        return {k: None for k in ['utm_source','utm_medium','utm_campaign','utm_content','utm_term']}
    try:
        p = parse_qs(urlparse(landing_site).query)
        return {
            'utm_source':   p.get('utm_source',   [None])[0],
            'utm_medium':   p.get('utm_medium',   [None])[0],
            'utm_campaign': p.get('utm_campaign', [None])[0],
            'utm_content':  p.get('utm_content',  [None])[0],
            'utm_term':     p.get('utm_term',     [None])[0],
        }
    except Exception:
        return {k: None for k in ['utm_source','utm_medium','utm_campaign','utm_content','utm_term']}


def calculate_shopify_fee(gateway, total_price):
    if gateway == 'shopify_payments':
        return round((total_price * SHOPIFY_PLAN_RATE) + SHOPIFY_PLAN_FIXED_FEE, 4)
    return None


def derive_fulfillment_status(order):
    """
    Derive fulfillment status from fulfillments array.
    Top-level fulfillment_status field is unreliable — sometimes null for fulfilled orders.
    """
    fulfillments = order.get('fulfillments', [])
    if fulfillments:
        statuses = [f.get('status') for f in fulfillments]
        if all(s == 'success' for s in statuses):
            return 'fulfilled'
        elif any(s == 'success' for s in statuses):
            return 'partial'
        else:
            return order.get('fulfillment_status') or 'unfulfilled'
    return order.get('fulfillment_status') or 'unfulfilled'


def extract_refunds(order):
    """
    Extract refund amount and gateway breakdown.
    Prefers GBP transactions. Falls back to total_price - current_total_price for non-GBP.
    """
    total_price   = float(order.get('total_price', 0))
    refunds       = order.get('refunds', [])
    refunded_at   = refunds[0].get('processed_at') if refunds else None
    refund_amount = 0.0
    gateway_breakdown = {}
    has_gbp_txn   = False

    for refund in refunds:
        for txn in refund.get('transactions', []):
            if txn.get('kind') == 'refund' and txn.get('status') == 'success':
                currency = txn.get('currency', '')
                amount   = float(txn.get('amount', 0))
                gw       = txn.get('gateway', 'unknown')
                if currency == 'GBP':
                    refund_amount += amount
                    gateway_breakdown[gw] = round(gateway_breakdown.get(gw, 0) + amount, 4)
                    has_gbp_txn = True

    if not has_gbp_txn and refunds:
        current_total = float(order.get('current_total_price', total_price) or total_price)
        refund_amount = round(total_price - current_total, 4)

    return {
        'refund_amount_gbp':        round(refund_amount, 4),
        'refund_by_gateway_json':   json.dumps(gateway_breakdown) if gateway_breakdown else None,
        'refunded_at':              refunded_at,
    }

# ─── FORMAT RECORDS ───────────────────────────────────────────────────────────

def _normalize_gateway(raw):
    """Collapse comma-joined gateway_names into a single primary_gateway.

    Priority: Klarna > PayPal > Shopify Payments > manual > raw. Mirrors the
    CASE in sql/migration_v8.14_primary_gateway.sql — keep them in sync.
    """
    if not raw:
        return raw
    lower = raw.lower()
    if 'klarna'           in lower: return 'klarna'
    if 'paypal'           in lower: return 'paypal'
    if 'shopify_payments' in lower: return 'shopify_payments'
    if 'manual'           in lower: return 'manual'
    return raw


def format_order(o):
    """Extract all order fields. Maps to orders table schema v7.0."""
    customer    = o.get('customer') or {}
    shipping    = o.get('shipping_address') or {}
    landing     = o.get('landing_site') or ''
    utms        = parse_utms(landing)

    total_price  = float(o.get('total_price', 0))
    # Shopify REST exposes the gateway via `payment_gateway_names` (list).
    # The previously-read `payment_gateway` (singular) does not exist and
    # always returned '' — leaving the column blank on all historical rows.
    gateway_names   = o.get('payment_gateway_names') or []
    gateway         = ','.join(gateway_names)
    primary_gateway = _normalize_gateway(gateway)

    # Shipping price from shipping lines
    shipping_lines = o.get('shipping_lines', [])
    shipping_price = sum(float(sl.get('price', 0)) for sl in shipping_lines)

    # Presentment currency
    price_set    = o.get('total_price_set') or {}
    presentment  = price_set.get('presentment_money') or {}

    # Discount codes
    discount_codes = o.get('discount_codes', [])
    discount_code  = discount_codes[0].get('code')   if discount_codes else None
    discount_type  = discount_codes[0].get('type')   if discount_codes else None
    discount_value = float(discount_codes[0].get('amount', 0)) if discount_codes else None

    # Refunds
    refund_data = extract_refunds(o)

    # Fulfillment status (derived — more reliable than top-level field)
    shopify_fulfillment_status = derive_fulfillment_status(o)

    # Cancellation
    cancelled_at  = o.get('cancelled_at')
    cancel_reason = o.get('cancel_reason')

    return {
        # Identity
        'order_id':                 str(o.get('id')),
        'brand_id':                 BRAND_ID,
        'created_at':               o.get('created_at'),
        'order_name':               o.get('name'),
        'synced_at':                datetime.now(timezone.utc).isoformat(),

        # Revenue
        'revenue_gbp':              total_price,
        'subtotal_gbp':             float(o.get('subtotal_price', 0)),
        'shipping_charged_gbp':     shipping_price,
        'total_tax_gbp':            float(o.get('total_tax', 0)),
        'tax_lines_json':           json.dumps(o.get('tax_lines', [])),
        'revenue_presentment':      float(presentment.get('amount', 0)) if presentment else None,
        'presentment_currency':     presentment.get('currency_code') if presentment else None,

        # Discounts
        'discount_amount_gbp':      float(o.get('total_discounts', 0)),
        'discount_code':            discount_code,
        'discount_type':            discount_type,
        'discount_value':           discount_value,

        # Payment & fees
        'payment_gateway':          gateway,
        'primary_gateway':          primary_gateway,
        'shopify_fee_gbp':          calculate_shopify_fee(gateway, total_price),
        'shopify_fee_pct':          SHOPIFY_PLAN_RATE if gateway == 'shopify_payments' else None,
        'paypal_settle_amount':     None,
        'paypal_fee_amount':        None,
        'klarna_fee_gbp':           None,

        # Shipping / location
        'shipping_country_code':    shipping.get('country_code'),
        'shipping_country_name':    shipping.get('country'),
        'shipping_province':        shipping.get('province'),
        'shipping_zip':             shipping.get('zip'),

        # Customer
        'customer_id':              str(customer.get('id', '')) or None,
        'customer_email':           (customer.get('email') or '').lower().strip() or None,
        'customer_name':            ' '.join(filter(None, [
                                        customer.get('first_name'),
                                        customer.get('last_name')
                                    ])) or None,
        'customer_orders_count':    customer.get('orders_count', 1),
        'is_new_customer':          customer.get('orders_count', 1) == 1,

        # UTM
        'landing_site':             landing or None,
        'referring_site':           o.get('referring_site') or None,
        'utm_source':               utms['utm_source'],
        'utm_medium':               utms['utm_medium'],
        'utm_campaign':             utms['utm_campaign'],
        'utm_content':              utms['utm_content'],
        'utm_term':                 utms['utm_term'],

        # COGS (populated by Gelato/Printify scripts)
        'cogs_gbp':                 None,
        'cogs_status':              'pending',
        'fulfillment_match_status': 'unmatched',
        'fulfillment_order_id':     None,

        # Financial status & refunds (Shopify = source of truth)
        'financial_status':             o.get('financial_status'),
        'refund_amount_gbp':            refund_data['refund_amount_gbp'],
        'refund_by_gateway_json':       refund_data['refund_by_gateway_json'],
        'refunded_at':                  refund_data['refunded_at'],
        'shopify_fulfillment_status':   shopify_fulfillment_status,
        'cancelled_at':                 cancelled_at,
        'cancel_reason':                cancel_reason,

        # System
        'line_items_count':         len(o.get('line_items', [])),
        'override_flag':            False,
    }


def format_line_items(o):
    items = []
    for li in o.get('line_items', []):
        unit_price = float(li.get('price', 0))
        quantity   = int(li.get('quantity', 1))
        items.append({
            'line_item_id':     str(li.get('id')),
            'order_id':         str(o.get('id')),
            'brand_id':         BRAND_ID,
            'product_id':       str(li.get('product_id', '')) or None,
            'variant_id':       str(li.get('variant_id', '')) or None,
            'product_title':    li.get('title'),
            'variant_title':    li.get('variant_title'),
            'quantity':         quantity,
            'unit_price_gbp':   unit_price,
            'line_total_gbp':   round(unit_price * quantity, 4),
            'line_cogs_gbp':    None,
        })
    return items

# ─── DATABASE UPSERTS ─────────────────────────────────────────────────────────

def upsert_order(conn, d):
    """Upsert order into orders table. Does not overwrite if override_flag=TRUE."""
    sql = """
        INSERT INTO orders (
            order_id, brand_id, created_at, order_name,
            revenue_gbp, subtotal_gbp, shipping_charged_gbp, total_tax_gbp,
            tax_lines_json, revenue_presentment, presentment_currency,
            discount_amount_gbp, discount_code, discount_type, discount_value,
            payment_gateway, primary_gateway, shopify_fee_gbp, shopify_fee_pct,
            paypal_settle_amount, paypal_fee_amount, klarna_fee_gbp,
            shipping_country_code, shipping_country_name, shipping_province, shipping_zip,
            customer_id, customer_email, customer_name,
            customer_orders_count, is_new_customer,
            landing_site, referring_site,
            utm_source, utm_medium, utm_campaign, utm_content, utm_term,
            cogs_gbp, cogs_status, fulfillment_match_status, fulfillment_order_id,
            financial_status, refund_amount_gbp, refund_by_gateway_json, refunded_at,
            shopify_fulfillment_status, cancelled_at, cancel_reason,
            line_items_count, override_flag, synced_at
        ) VALUES (
            %(order_id)s, %(brand_id)s, %(created_at)s, %(order_name)s,
            %(revenue_gbp)s, %(subtotal_gbp)s, %(shipping_charged_gbp)s, %(total_tax_gbp)s,
            %(tax_lines_json)s, %(revenue_presentment)s, %(presentment_currency)s,
            %(discount_amount_gbp)s, %(discount_code)s, %(discount_type)s, %(discount_value)s,
            %(payment_gateway)s, %(primary_gateway)s, %(shopify_fee_gbp)s, %(shopify_fee_pct)s,
            %(paypal_settle_amount)s, %(paypal_fee_amount)s, %(klarna_fee_gbp)s,
            %(shipping_country_code)s, %(shipping_country_name)s, %(shipping_province)s, %(shipping_zip)s,
            %(customer_id)s, %(customer_email)s, %(customer_name)s,
            %(customer_orders_count)s, %(is_new_customer)s,
            %(landing_site)s, %(referring_site)s,
            %(utm_source)s, %(utm_medium)s, %(utm_campaign)s, %(utm_content)s, %(utm_term)s,
            %(cogs_gbp)s, %(cogs_status)s, %(fulfillment_match_status)s, %(fulfillment_order_id)s,
            %(financial_status)s, %(refund_amount_gbp)s, %(refund_by_gateway_json)s, %(refunded_at)s,
            %(shopify_fulfillment_status)s, %(cancelled_at)s, %(cancel_reason)s,
            %(line_items_count)s, %(override_flag)s, %(synced_at)s
        )
        ON CONFLICT (order_id) DO UPDATE SET
            order_name                  = EXCLUDED.order_name,
            revenue_gbp                 = EXCLUDED.revenue_gbp,
            subtotal_gbp                = EXCLUDED.subtotal_gbp,
            shipping_charged_gbp        = EXCLUDED.shipping_charged_gbp,
            total_tax_gbp               = EXCLUDED.total_tax_gbp,
            tax_lines_json              = EXCLUDED.tax_lines_json,
            revenue_presentment         = EXCLUDED.revenue_presentment,
            presentment_currency        = EXCLUDED.presentment_currency,
            discount_amount_gbp         = EXCLUDED.discount_amount_gbp,
            discount_code               = EXCLUDED.discount_code,
            discount_type               = EXCLUDED.discount_type,
            discount_value              = EXCLUDED.discount_value,
            payment_gateway             = EXCLUDED.payment_gateway,
            primary_gateway             = EXCLUDED.primary_gateway,
            shopify_fee_gbp             = EXCLUDED.shopify_fee_gbp,
            shopify_fee_pct             = EXCLUDED.shopify_fee_pct,
            shipping_country_code       = EXCLUDED.shipping_country_code,
            shipping_country_name       = EXCLUDED.shipping_country_name,
            shipping_province           = EXCLUDED.shipping_province,
            shipping_zip                = EXCLUDED.shipping_zip,
            customer_id                 = EXCLUDED.customer_id,
            customer_email              = EXCLUDED.customer_email,
            customer_name               = EXCLUDED.customer_name,
            customer_orders_count       = EXCLUDED.customer_orders_count,
            is_new_customer             = EXCLUDED.is_new_customer,
            landing_site                = EXCLUDED.landing_site,
            referring_site              = EXCLUDED.referring_site,
            utm_source                  = EXCLUDED.utm_source,
            utm_medium                  = EXCLUDED.utm_medium,
            utm_campaign                = EXCLUDED.utm_campaign,
            utm_content                 = EXCLUDED.utm_content,
            utm_term                    = EXCLUDED.utm_term,
            line_items_count            = EXCLUDED.line_items_count,
            financial_status            = EXCLUDED.financial_status,
            refund_amount_gbp           = EXCLUDED.refund_amount_gbp,
            refund_by_gateway_json      = EXCLUDED.refund_by_gateway_json,
            refunded_at                 = COALESCE(EXCLUDED.refunded_at, orders.refunded_at),
            shopify_fulfillment_status  = EXCLUDED.shopify_fulfillment_status,
            cancelled_at                = EXCLUDED.cancelled_at,
            cancel_reason               = EXCLUDED.cancel_reason,
            synced_at                   = EXCLUDED.synced_at
        WHERE orders.override_flag = FALSE
    """
    with conn.cursor() as cur:
        cur.execute(sql, d)


def upsert_line_items(conn, line_items):
    if not line_items:
        return
    sql = """
        INSERT INTO line_items (
            line_item_id, order_id, brand_id,
            product_id, variant_id, product_title, variant_title,
            quantity, unit_price_gbp, line_total_gbp, line_cogs_gbp
        ) VALUES %s
        ON CONFLICT (line_item_id) DO UPDATE SET
            product_title   = EXCLUDED.product_title,
            variant_title   = EXCLUDED.variant_title,
            quantity        = EXCLUDED.quantity,
            unit_price_gbp  = EXCLUDED.unit_price_gbp,
            line_total_gbp  = EXCLUDED.line_total_gbp
    """
    execute_values(conn.cursor(), sql, [
        (
            li['line_item_id'], li['order_id'], li['brand_id'],
            li['product_id'], li['variant_id'], li['product_title'], li['variant_title'],
            li['quantity'], li['unit_price_gbp'], li['line_total_gbp'], li['line_cogs_gbp']
        )
        for li in line_items
    ])


def upsert_customer(conn, customer_id, customer_email, brand_id, first_seen_at):
    """Basic customer record — full sync handled by customers_sync.py."""
    if not customer_id:
        return
    sql = """
        INSERT INTO customers (customer_id, brand_id, customer_email, first_seen_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (customer_id, brand_id) DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(sql, (customer_id, brand_id, customer_email, first_seen_at, first_seen_at))

# ─── SYNC ─────────────────────────────────────────────────────────────────────

def run_sync(lookback_days=DEFAULT_LOOKBACK, updated_at_max=None):
    test_db_connection()
    conn = get_db_connection()

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    orders = fetch_orders(
        updated_at_min=cutoff.isoformat(),
        updated_at_max=updated_at_max,
    )

    processed = errors = 0

    try:
        for o in orders:
            try:
                order_data  = format_order(o)
                line_items  = format_line_items(o)
                customer    = o.get('customer') or {}
                customer_id = str(customer.get('id', '')) or None
                email       = (customer.get('email') or '').lower().strip() or None

                upsert_order(conn, order_data)
                upsert_line_items(conn, line_items)
                if customer_id:
                    upsert_customer(conn, customer_id, email, BRAND_ID, o.get('created_at'))

                conn.commit()
                processed += 1
                logger.debug(f'Synced order {order_data["order_name"]} ({order_data["order_id"]})')

            except Exception as e:
                conn.rollback()
                logger.error(f'Failed to sync order {o.get("id")}: {e}')
                errors += 1

    finally:
        conn.close()

    logger.info(f'Sync complete — orders processed: {processed} | errors: {errors}')
    return processed, errors

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Shopify → Postgres sync')
    parser.add_argument('--lookback-days', type=int, default=DEFAULT_LOOKBACK,
                        help='Days to look back using updated_at (default: 3)')
    args = parser.parse_args()

    logger.info(f'Incremental sync: lookback {args.lookback_days} days from '
                f'{(datetime.now(timezone.utc) - timedelta(days=args.lookback_days)).isoformat()}')

    processed, errors = run_sync(lookback_days=args.lookback_days)
    logger.info('Script complete')
    sys.exit(1 if errors > 0 else 0)
