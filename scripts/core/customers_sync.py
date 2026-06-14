"""
customers_sync.py
=================
Syncs Shopify customer data to the customers table.
Captures orders_count, total_spent, email marketing consent, LTV data.

Run modes:
    python customers_sync.py                    # last 7 days updated customers
    python customers_sync.py --full-backfill    # all 21,401 customers
    python customers_sync.py --days 30          # custom lookback

Cron: 30 3 * * * (after Shopify, Gelato, Printify, before summary)
"""

import os
import sys
import logging
import argparse
import requests
import psycopg2
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv("/opt/your_brand_id/.env")

SHOPIFY_STORE   = os.getenv('SHOPIFY_STORE_NAME')
SHOPIFY_TOKEN   = os.getenv('SHOPIFY_ACCESS_TOKEN')
SHOPIFY_API_URL = f'https://{SHOPIFY_STORE}.myshopify.com/admin/api/2025-04'

DB_HOST     = os.getenv('DB_HOST', 'localhost')
DB_PORT     = os.getenv('DB_PORT', '5432')
DB_NAME     = os.getenv('DB_NAME')
DB_USER     = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')
BRAND_ID    = os.getenv('BRAND_ID', 'your_brand_id')

REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '30'))
DEFAULT_LOOKBACK = 7

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [customers_sync] [%(levelname)s] %(message)s'
)
logger = logging.getLogger('customers_sync')

# ─── DATABASE ─────────────────────────────────────────────────────────────────

def get_db_connection():
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD
    )
    conn.autocommit = False
    return conn

# ─── SHOPIFY API ──────────────────────────────────────────────────────────────

HEADERS = {'X-Shopify-Access-Token': SHOPIFY_TOKEN}

def fetch_customers_page(params):
    """Fetch one page of customers."""
    r = requests.get(
        f'{SHOPIFY_API_URL}/customers.json',
        headers=HEADERS,
        params=params,
        timeout=REQUEST_TIMEOUT
    )
    r.raise_for_status()
    # Get next page link if available
    next_link = None
    link_header = r.headers.get('Link', '')
    if 'rel="next"' in link_header:
        for part in link_header.split(','):
            if 'rel="next"' in part:
                next_link = part.split(';')[0].strip().strip('<>')
                break
    return r.json().get('customers', []), next_link


def fetch_all_customers(updated_at_min=None):
    """
    Fetch all customers via pagination.
    If updated_at_min provided, only fetch customers updated since then.
    """
    params = {'limit': 250}
    if updated_at_min:
        params['updated_at_min'] = updated_at_min

    all_customers = []
    page = 1
    next_url = None

    while True:
        if next_url:
            r = requests.get(next_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            customers = r.json().get('customers', [])
            link_header = r.headers.get('Link', '')
            next_url = None
            if 'rel="next"' in link_header:
                for part in link_header.split(','):
                    if 'rel="next"' in part:
                        next_url = part.split(';')[0].strip().strip('<>')
                        break
        else:
            customers, next_url = fetch_customers_page(params)

        all_customers.extend(customers)
        logger.debug(f'Page {page}: {len(customers)} customers fetched')
        page += 1

        if not next_url:
            break

    logger.info(f'Total customers fetched: {len(all_customers)}')
    return all_customers

# ─── DATABASE OPERATIONS ──────────────────────────────────────────────────────

def upsert_customer(conn, customer):
    """Upsert a single customer record."""
    customer_id = str(customer.get('id', ''))
    if not customer_id:
        return False

    email       = (customer.get('email') or '').lower().strip() or None
    phone       = customer.get('phone') or None
    first_name  = customer.get('first_name') or None
    last_name   = customer.get('last_name') or None
    orders_count = int(customer.get('orders_count') or 0)
    total_spent  = float(customer.get('total_spent') or 0)
    created_at   = customer.get('created_at')
    updated_at   = customer.get('updated_at')

    # Marketing consent (Shopify exposes structured per-channel objects).
    # Falls back gracefully if older API response shape is in play.
    email_consent = customer.get('email_marketing_consent') or {}
    sms_consent   = customer.get('sms_marketing_consent') or {}

    email_marketing_state        = email_consent.get('state')
    email_marketing_opt_in_level = email_consent.get('opt_in_level')
    email_consent_updated_at     = email_consent.get('consent_updated_at')
    sms_marketing_state          = sms_consent.get('state')
    sms_marketing_opt_in_level   = sms_consent.get('opt_in_level')
    sms_consent_updated_at       = sms_consent.get('consent_updated_at')

    sql = """
        INSERT INTO customers (
            customer_id, brand_id,
            customer_email, customer_phone,
            first_name, last_name,
            first_seen_at, last_seen_at,
            total_orders, total_revenue_gbp,
            revenue_ltv, ltv_updated_at,
            email_marketing_state, email_marketing_opt_in_level, email_consent_updated_at,
            sms_marketing_state, sms_marketing_opt_in_level, sms_consent_updated_at
        ) VALUES (
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s,
            %s, %s, %s
        )
        ON CONFLICT (customer_id, brand_id) DO UPDATE SET
            customer_email                = COALESCE(EXCLUDED.customer_email, customers.customer_email),
            customer_phone                = COALESCE(EXCLUDED.customer_phone, customers.customer_phone),
            first_name                    = COALESCE(EXCLUDED.first_name, customers.first_name),
            last_name                     = COALESCE(EXCLUDED.last_name, customers.last_name),
            last_seen_at                  = EXCLUDED.last_seen_at,
            total_orders                  = EXCLUDED.total_orders,
            total_revenue_gbp             = EXCLUDED.total_revenue_gbp,
            revenue_ltv                   = EXCLUDED.total_revenue_gbp,
            ltv_updated_at                = EXCLUDED.ltv_updated_at,
            email_marketing_state         = COALESCE(EXCLUDED.email_marketing_state,        customers.email_marketing_state),
            email_marketing_opt_in_level  = COALESCE(EXCLUDED.email_marketing_opt_in_level, customers.email_marketing_opt_in_level),
            email_consent_updated_at      = COALESCE(EXCLUDED.email_consent_updated_at,     customers.email_consent_updated_at),
            sms_marketing_state           = COALESCE(EXCLUDED.sms_marketing_state,          customers.sms_marketing_state),
            sms_marketing_opt_in_level    = COALESCE(EXCLUDED.sms_marketing_opt_in_level,   customers.sms_marketing_opt_in_level),
            sms_consent_updated_at        = COALESCE(EXCLUDED.sms_consent_updated_at,       customers.sms_consent_updated_at)
    """

    with conn.cursor() as cur:
        cur.execute(sql, (
            customer_id,
            BRAND_ID,
            email,
            phone,
            first_name,
            last_name,
            created_at,
            updated_at,
            orders_count,
            total_spent,
            total_spent,
            datetime.now(timezone.utc).isoformat(),
            email_marketing_state, email_marketing_opt_in_level, email_consent_updated_at,
            sms_marketing_state, sms_marketing_opt_in_level, sms_consent_updated_at,
        ))
    return True

def update_is_new_customer(conn):
    logger.info('Updating is_new_customer on orders table...')
    with conn.cursor() as cur:
        cur.execute("""
            WITH ranked AS (
                SELECT
                    order_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY customer_id, brand_id
                        ORDER BY created_at ASC
                    ) as order_sequence
                FROM orders
                WHERE brand_id = %s
                AND customer_id IS NOT NULL
            )
            UPDATE orders o
            SET is_new_customer = CASE WHEN r.order_sequence = 1 THEN TRUE ELSE FALSE END
            FROM ranked r
            WHERE o.order_id = r.order_id
            AND o.override_flag = FALSE
        """, (BRAND_ID,))
        updated = cur.rowcount
    logger.info(f'is_new_customer updated for {updated} orders')
    
# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run_sync(full_backfill=False, days=DEFAULT_LOOKBACK):
    conn = get_db_connection()

    if full_backfill:
        logger.info('Full backfill mode — fetching all customers')
        updated_at_min = None
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        updated_at_min = cutoff.isoformat()
        logger.info(f'Incremental sync — customers updated since {cutoff.date()}')

    customers = fetch_all_customers(updated_at_min=updated_at_min)

    synced = errors = skipped = 0

    try:
        for customer in customers:
            try:
                if upsert_customer(conn, customer):
                    synced += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f'Failed for {customer.get("email")}: {e}')
                conn.rollback()
                errors += 1

        conn.commit()
        logger.info(f'Customer sync complete — synced: {synced} | skipped: {skipped} | errors: {errors}')

        # Update is_new_customer on orders table
        update_is_new_customer(conn)
        conn.commit()

    except Exception as e:
        conn.rollback()
        logger.error(f'Sync failed: {e}')
        sys.exit(1)
    finally:
        conn.close()

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Shopify customers → Postgres sync')
    parser.add_argument('--full-backfill', action='store_true',
                        help='Fetch all customers (one-off backfill)')
    parser.add_argument('--days', type=int, default=DEFAULT_LOOKBACK,
                        help='Lookback days for incremental sync (default: 7)')
    args = parser.parse_args()

    logger.info('Customers sync starting')
    run_sync(full_backfill=args.full_backfill, days=args.days)
    logger.info('Script complete')
    sys.exit(0)
