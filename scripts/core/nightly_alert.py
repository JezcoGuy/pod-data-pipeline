"""
nightly_alert.py
================
Database-driven consolidated nightly alert email.
Queries the database directly — no scratch file needed.

Runs at 4am via cron AFTER all sync scripts have completed.
Sync scripts are pure data ingestion — all alert logic lives here.

Usage if in directory:
    python3 nightly_alert.py
    python3 nightly_alert.py --test     # send without side effects
    python3 nightly_alert.py --dry-run  # print to console only

Outside of directory example:
    python3 /opt/your_brand_id/scripts/nightly_alert.py --dry-run
"""

import os
import sys
import smtplib
import logging
import argparse
import psycopg2
from datetime import datetime, timezone
from email.message import EmailMessage
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv()

SMTP_HOST   = os.getenv('SMTP_HOST')
# Use 587 (STARTTLS) by default — works on most VPS providers. 465 is
# implicit SSL and is blocked by some providers (e.g. Hetzner outbound).
SMTP_PORT   = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER   = os.getenv('SMTP_USER') or os.getenv('SMTP_FROM')
SMTP_PASS   = os.getenv('SMTP_PASS')
SMTP_FROM   = os.getenv('SMTP_FROM') or SMTP_USER
SMTP_TO     = os.getenv('SMTP_TO')
BRAND_ID    = os.getenv('BRAND_ID', 'your_brand_id')

DB_HOST     = os.getenv('DB_HOST', 'localhost')
DB_PORT     = os.getenv('DB_PORT', '5432')
DB_NAME     = os.getenv('DB_NAME')
DB_USER     = os.getenv('DB_USER')
DB_PASSWORD = os.getenv('DB_PASSWORD')

COGS_ALERT_HOURS    = int(os.getenv('GELATO_ALERT_HOURS', '48'))
RETURNED_ALERT_DAYS = int(os.getenv('RETURNED_ALERT_DAYS', '60'))
LATE_DELIVERY_DAYS  = int(os.getenv('LATE_DELIVERY_DAYS', '7'))
REFUND_ALERT_DAYS   = int(os.getenv('REFUND_ALERT_DAYS', '7'))

# ─── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [nightly_alert] [%(levelname)s] %(message)s'
)
logger = logging.getLogger('nightly_alert')

# ─── DATABASE ─────────────────────────────────────────────────────────────────

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD
    )

# ─── ALERT CHECKS ─────────────────────────────────────────────────────────────

def check_unmatched_cogs(conn):
    """Orders > 48h with no fulfilment COGS match."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT order_name, revenue_gbp, created_at::date
            FROM orders
            WHERE brand_id = %s
              AND fulfillment_match_status = 'unmatched'
              AND created_at < NOW() - INTERVAL '48 hours'
              AND override_flag = FALSE
            ORDER BY created_at DESC
        """, (BRAND_ID,))
        rows = cur.fetchall()

    if not rows:
        return None

    return {
        'title': f"\u26a0\ufe0f  UNMATCHED COGS — {len(rows)} orders > {COGS_ALERT_HOURS}h with no fulfilment match",
        'lines': [f"  {r[0]} | \u00a3{r[1]:.2f} | {r[2]}" for r in rows],
        'count': len(rows),
    }


def check_returned_orders(conn):
    """Orders returned to sender within last 60 days."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                o.order_name, o.shipping_country_name,
                f.tracking_number, f.carrier, f.dispatched_at::date
            FROM fulfilments f
            JOIN orders o ON o.order_id = f.shopify_order_id
            WHERE f.provider = 'gelato'
              AND f.brand_id = %s
              AND f.fulfilment_status = 'returned'
              AND f.dispatched_at >= NOW() - INTERVAL '60 days'
              AND f.override_flag = FALSE
            ORDER BY f.dispatched_at DESC
        """, (BRAND_ID,))
        rows = cur.fetchall()

    if not rows:
        return None

    lines = [f"  {r[0]} | {r[1]} | {r[2]} | {r[3]} | dispatched {r[4]}" for r in rows]
    lines.append("")
    lines.append("  To resolve: NocoDB \u2192 fulfilments \u2192 set fulfilment_status='returned_resolved' + override_flag=TRUE")

    return {
        'title': f"\U0001f4e6  RETURNED TO SENDER — {len(rows)} order(s) need attention",
        'lines': lines,
        'count': len(rows),
    }


def check_late_deliveries(conn):
    """Orders dispatched > 7 days ago not yet delivered."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                order_name, shipping_country_name,
                days_since_dispatch, tracking_number, carrier, provider
            FROM order_fulfilment_status
            WHERE is_late = TRUE
              AND brand_id = %s
            ORDER BY days_since_dispatch DESC
        """, (BRAND_ID,))
        rows = cur.fetchall()

    if not rows:
        return None

    return {
        'title': f"\U0001f550  LATE DELIVERIES — {len(rows)} order(s) dispatched > {LATE_DELIVERY_DAYS} days ago",
        'lines': (
                    [f"  {r[0]} | {r[1]} | {r[2]} days | {r[3]} | {r[4]} | {r[5]}" for r in rows] + [
                        "",
                        "  HOW TO RESOLVE:",
                        "  1. Check order in Shopify and Gelato dashboard",
                        "  2. If delivered — update fulfillment status in Shopify to Fulfilled",
                        "  3. Don't forget to update Shopify once issue is resolved with customer",
                        "     This will clear the order from future alerts automatically",
                        "  4. If Shopify/Gelato sync error only — NocoDB → fulfilments → set override_flag=TRUE",
                    ]
                ),        
        'count': len(rows),
    }


def check_refunds_no_return_entry(conn):
    """Refunded orders in last 7 days with no returns table entry."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                o.order_name, o.customer_name,
                o.shipping_country_name, o.refund_amount_gbp,
                o.financial_status, o.refunded_at::date
            FROM orders o
            LEFT JOIN returns r ON r.order_id = o.order_id
            WHERE o.brand_id = %s
              AND o.financial_status IN ('refunded', 'partially_refunded')
              AND o.refunded_at >= NOW() - INTERVAL '7 days'
              AND r.return_id IS NULL
            ORDER BY o.refunded_at DESC
        """, (BRAND_ID,))
        rows = cur.fetchall()

    if not rows:
        return None

    lines = [f"  {r[0]} | {r[1]} | {r[2]} | \u00a3{r[3]:.2f} | {r[4]} | {r[5]}" for r in rows]
    lines.append("")
    lines.append("  Please log return details in NocoDB \u2192 returns table")

    return {
        'title': f"\U0001f4b3  REFUNDS WITHOUT RETURN ENTRY — {len(rows)} order(s) need logging",
        'lines': lines,
        'count': len(rows),
    }


def check_partially_paid(conn):
    """Orders with outstanding payment balances."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT order_name, customer_name, revenue_gbp, created_at::date
            FROM orders
            WHERE brand_id = %s
              AND financial_status = 'partially_paid'
            ORDER BY created_at DESC
        """, (BRAND_ID,))
        rows = cur.fetchall()

    if not rows:
        return None

    return {
        'title': f"\U0001f4b0  PARTIALLY PAID — {len(rows)} order(s) with outstanding balances",
        'lines': ([f"  {r[0]} | {r[1]} | \u00a3{r[2]:.2f} | {r[3]}" for r in rows] + [
            "",
                        "  HOW TO RESOLVE:",
                        "  1. Check order in Shopify dashboard",
                        "  2. If order is settled or has very little outstanding, choose mark as paid in the dropdown",
                        "  3. Investigate further if partial payment is considerable",
                        "  4. Anything else - NocoDB → fulfilments → set override_flag=TRUE",
                    ]),
        'count': len(rows),
    }


def check_pagespeed_regression(conn):
    """Alert if any audited URL's MOBILE performance score has dropped
    >10 points vs the average of the previous 3 days.

    Why mobile only: desktop scores are typically pinned at 100; mobile
    is where real-world regressions surface (image bloat, theme updates,
    slow third-party scripts). Why 10 points: PageSpeed scores fluctuate
    ±3-5 naturally between audits; >10 indicates a real regression.

    Silently returns None if there's no historical baseline yet (first
    few days after pagespeed_sync.py started running).
    """
    with conn.cursor() as cur:
        cur.execute("""
            WITH latest AS (
                SELECT DISTINCT ON (page_url)
                    page_url, page_path, date, score_performance
                FROM pagespeed_daily
                WHERE brand_id = %s
                  AND strategy = 'mobile'
                  AND score_performance IS NOT NULL
                ORDER BY page_url, date DESC
            ),
            baseline AS (
                SELECT
                    p.page_url,
                    AVG(p.score_performance)::numeric AS avg_prior_3d,
                    COUNT(*) AS prior_days
                FROM pagespeed_daily p
                JOIN latest l ON l.page_url = p.page_url
                WHERE p.brand_id = %s
                  AND p.strategy = 'mobile'
                  AND p.score_performance IS NOT NULL
                  AND p.date < l.date
                  AND p.date >= l.date - INTERVAL '3 days'
                GROUP BY p.page_url
                HAVING COUNT(*) >= 1
            )
            SELECT
                l.page_path,
                l.date,
                l.score_performance                                AS today_score,
                ROUND(b.avg_prior_3d)::int                         AS avg_3d_score,
                ROUND(b.avg_prior_3d - l.score_performance)::int   AS drop_points,
                b.prior_days
            FROM latest l
            JOIN baseline b ON b.page_url = l.page_url
            WHERE l.score_performance < b.avg_prior_3d - 10
            ORDER BY drop_points DESC
        """, (BRAND_ID, BRAND_ID))
        rows = cur.fetchall()

    if not rows:
        return None

    lines = [
        f"  {r[0]} | today: {r[2]}/100 | prior {r[5]}d avg: {r[3]} | drop: -{r[4]} points"
        for r in rows
    ]
    lines.append("")
    lines.append("  Run Lighthouse manually for detail: https://pagespeed.web.dev/")
    lines.append("  Common causes: large image upload, theme update, new third-party script,")
    lines.append("                 broken CDN config, font swap.")

    return {
        'title': f"⚡  PAGESPEED MOBILE REGRESSION — {len(rows)} URL(s) dropped >10 points",
        'lines': lines,
        'count': len(rows),
    }


def check_best_seller_sync(conn):
    """Append best seller tag sync summary if a run happened in the last 24h.

    Written by /opt/your_brand_id/best_seller_update/best_seller_sync.py --execute
    (Tue + Fri 13:00). Returns the block for the email body; only includes
    it the morning after a scheduled run, so the same summary isn't repeated
    across multiple days.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT run_at, total_currently_tagged, adds_count, removes_count,
                   keeps_count, added_products, removed_products
            FROM best_seller_sync_runs
            WHERE brand_id = %s
              AND mode = 'execute'
              AND run_at >= NOW() - INTERVAL '24 hours'
            ORDER BY run_at DESC
            LIMIT 1
        """, (BRAND_ID,))
        row = cur.fetchone()

    if not row:
        return None

    run_at, total_tagged, adds, removes, keeps, added_products, removed_products = row
    run_display = run_at.astimezone().strftime('%d %b %Y %H:%M')

    added_names   = [p["design"] for p in (added_products   or [])]
    removed_names = [p["design"] for p in (removed_products or [])]

    lines = [
        f"  Run: {run_display}",
        f"  Total tagged: {total_tagged} products",
        "",
    ]
    if adds + removes == 0:
        lines.append(f"  ✅ No changes — {keeps} products unchanged")
    else:
        if added_names:
            lines.append(f"  ✅ Added ({adds}): " + ", ".join(added_names))
        if removed_names:
            lines.append(f"  ❌ Removed ({removes}): " + ", ".join(removed_names))
        lines.append(f"  \U0001f504 Unchanged: {keeps} products")

    return {
        'title': f"\U0001f3f7️  BEST SELLER TAG SYNC — {adds + removes} change(s)",
        'lines': lines,
        'count': adds + removes,
    }


# ─── FUTURE PLACEHOLDERS ──────────────────────────────────────────────────────
# Add new check functions here as new scripts come online:
# def check_meta_spend_anomaly(conn): ...
# def check_klaviyo_list_drop(conn): ...
# def check_gsc_query_drop(conn): ...  # alert if organic clicks halve vs 7-day avg

# ─── BUILD & SEND ─────────────────────────────────────────────────────────────

def build_email_body(results):
    date_str     = datetime.now().strftime('%A %d %B %Y')
    total_alerts = sum(r['count'] for r in results)

    lines = [
        "Your Brand Nightly Alert",
        f"Date: {date_str}",
        f"Total items requiring attention: {total_alerts}",
        "=" * 60, "",
    ]
    for result in results:
        lines.append(result['title'])
        lines.append("-" * 60)
        lines.extend(result['lines'])
        lines.append("")
    lines.extend([
        "=" * 60,
        "Your Brand Data Pipeline — automated nightly report.",
        "Cron failure alerts are sent immediately and separately.",
    ])
    return "\n".join(lines)


def send_email(body, test_mode=False):
    date_str = datetime.now().strftime('%A %d %B %Y')
    subject  = f'[Your Brand] Nightly Alert \u2014 {date_str}'
    if test_mode:
        subject = f'[TEST] {subject}'

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From']    = SMTP_FROM
    msg['To']      = SMTP_TO
    msg.set_content(body)

    try:
        if SMTP_PORT == 465:
            # Implicit SSL
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        else:
            # STARTTLS (port 587 or other) — preferred
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.ehlo()
                server.starttls()
                server.login(SMTP_USER, SMTP_PASS)
                server.send_message(msg)
        logger.info(f'Alert sent to {SMTP_TO}')
        return True
    except Exception as e:
        logger.error(f'Failed to send: {e}')
        return False

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Nightly consolidated alert')
    parser.add_argument('--test',    action='store_true', help='Send test email')
    parser.add_argument('--dry-run', action='store_true', help='Print to console only')
    args = parser.parse_args()

    logger.info('Nightly alert starting')

    conn    = get_db_connection()
    results = []

    try:
        checks = [
            check_unmatched_cogs,
            check_returned_orders,
            check_late_deliveries,
            check_refunds_no_return_entry,
            check_partially_paid,
            check_pagespeed_regression,
            check_best_seller_sync,
        ]
        for check in checks:
            try:
                result = check(conn)
                if result:
                    results.append(result)
                    logger.info(f'{check.__name__}: {result["count"]} item(s)')
                else:
                    logger.info(f'{check.__name__}: nothing to report')
            except Exception as e:
                logger.error(f'{check.__name__} failed: {e}')
    finally:
        conn.close()

    if not results:
        logger.info('All clear — no email sent')
        sys.exit(0)

    body = build_email_body(results)

    if args.dry_run:
        print(body)
        logger.info('Dry run — no email sent')
        sys.exit(0)

    success = send_email(body, test_mode=args.test)
    logger.info('Complete')
    sys.exit(0 if success else 1)
