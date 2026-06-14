"""
klaviyo_sync.py
===============
Syncs Klaviyo campaign, flow, and sign-up form metrics to PostgreSQL.
Pure data ingestion — no alert logic.

Three reports per run, written daily:
  1. Campaigns -> email_campaigns (campaign_type='campaign')
  2. Flows     -> email_campaigns (campaign_type='flow')
  3. Forms     -> klaviyo_forms_daily

Uses Klaviyo's *-values-reports endpoints, called once per day in the
lookback window so each row in our DB represents one day of activity per
campaign / flow / form. ~90 API calls for a 30-day default lookback,
well under Klaviyo's per-second / per-minute rate limits.

Customer subscription state is intentionally NOT pulled from Klaviyo —
Shopify is source of truth (see customers.email_marketing_state, populated
by customers_sync.py).

Usage:
    python3 klaviyo_sync.py                          # last 30 days (cron default)
    python3 klaviyo_sync.py --lookback-days 90       # wider backfill
    python3 klaviyo_sync.py --dry-run                # fetch + summarise, no writes
"""

import os
import sys
import time
import logging
import argparse
from datetime import datetime, timedelta, timezone
import requests
import psycopg2
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv("/opt/your_brand_id/.env")

KLAVIYO_API_KEY   = os.getenv("KLAVIYO_API_KEY")
KLAVIYO_REVISION  = os.getenv("KLAVIYO_API_REVISION", "2024-10-15")

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

BRAND_ID          = os.getenv("BRAND_ID", "your_brand_id")
DEFAULT_LOOKBACK  = int(os.getenv("KLAVIYO_LOOKBACK_DAYS", "7"))
REQUEST_TIMEOUT   = int(os.getenv("KLAVIYO_TIMEOUT", "60"))
LOG_FILE          = os.getenv("KLAVIYO_LOG_FILE", "logs/klaviyo_sync.log")

KLAVIYO_BASE = "https://a.klaviyo.com/api"

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)

logger = logging.getLogger("klaviyo_sync")
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

# ─── KLAVIYO HTTP ─────────────────────────────────────────────────────────────

def _headers():
    return {
        "Authorization": f"Klaviyo-API-Key {KLAVIYO_API_KEY}",
        "Accept":        "application/vnd.api+json",
        "Content-Type":  "application/vnd.api+json",
        "revision":      KLAVIYO_REVISION,
    }


def request_with_retry(method, url, json_body=None, params=None, max_retries=5):
    """HTTP wrapper with rate-limit + 5xx retry."""
    backoff = 2
    for attempt in range(max_retries):
        try:
            resp = requests.request(
                method, url,
                headers=_headers(),
                json=json_body,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", backoff))
                logger.warning(f"Klaviyo rate-limited — retry in {wait}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue
            if resp.status_code >= 500:
                logger.warning(
                    f"Klaviyo {resp.status_code} (attempt {attempt+1}) — retry in {backoff}s. "
                    f"Body: {resp.text[:200]}"
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            if resp.status_code >= 400:
                # 400s are usually permanent — don't retry, surface immediately
                logger.error(f"Klaviyo {resp.status_code}: {resp.text[:400]}")
                resp.raise_for_status()
            time.sleep(0.15)  # gentle pacing
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            # Klaviyo's values-reports endpoint sometimes takes 30+ seconds
            # to respond, especially for older data. Treat timeouts as
            # retriable just like connection errors.
            logger.warning(f"Klaviyo connection/timeout error (attempt {attempt+1}): {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    raise Exception(f"Klaviyo request failed after {max_retries} attempts: {url}")

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def safe_int(v, default=0):
    if v is None:
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def safe_float(v, default=0.0):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def get_conversion_metric_id():
    """Discover the 'Placed Order' metric ID — required for values-reports calls.

    Klaviyo's /metrics/ doesn't let us filter by name directly (only by
    integration.name/integration.category). Listing is cheap (~10-20 metrics
    per account) so we just scan client-side. Paginates via links.next.
    """
    url = f"{KLAVIYO_BASE}/metrics/"
    while url:
        resp = request_with_retry("GET", url)
        body = resp.json()
        for m in body.get("data", []):
            if (m.get("attributes") or {}).get("name") == "Placed Order":
                return m["id"]
        url = (body.get("links") or {}).get("next")
    raise Exception("Could not find 'Placed Order' metric — check Klaviyo Shopify integration")


def _fetch_id_name_map(url, params, label):
    """Paginate a Klaviyo list endpoint and return dict {id: name}.

    Used to resolve campaign / flow / form IDs to human-readable names,
    which the *-values-reports endpoints don't include in their groupings.
    Failures are logged but non-fatal — sync still works without names.
    """
    names = {}
    next_url = url
    next_params = params
    try:
        while next_url:
            resp = request_with_retry("GET", next_url, params=next_params)
            body = resp.json()
            for item in body.get("data", []):
                iid = item.get("id")
                name = (item.get("attributes") or {}).get("name")
                if iid:
                    names[iid] = name
            next_url = (body.get("links") or {}).get("next")
            next_params = None  # next URL embeds params
    except Exception as e:
        logger.warning(f"{label} name lookup failed (continuing without names): {e}")
    logger.info(f"  resolved {len(names)} {label} names")
    return names


def fetch_campaign_names():
    """All email campaigns -> {campaign_id: name}.
    Klaviyo's /campaigns/ requires a filter on messages.channel AND does
    not accept a page[size] parameter (use Klaviyo's default)."""
    return _fetch_id_name_map(
        f"{KLAVIYO_BASE}/campaigns/",
        {
            "filter":          "equals(messages.channel,'email')",
            "fields[campaign]": "name",
        },
        "campaign",
    )


def fetch_flow_names():
    """All flows -> {flow_id: name}. /flows/ max page[size] is 50."""
    return _fetch_id_name_map(
        f"{KLAVIYO_BASE}/flows/",
        {"fields[flow]": "name", "page[size]": 50},
        "flow",
    )


def fetch_form_names():
    """All forms -> {form_id: name}. /forms/ accepts page[size] up to 100."""
    return _fetch_id_name_map(
        f"{KLAVIYO_BASE}/forms/",
        {"fields[form]": "name", "page[size]": 100},
        "form",
    )

# ─── REPORT FETCHERS ──────────────────────────────────────────────────────────

CAMPAIGN_STATS = [
    "opens",
    "opens_unique",
    "clicks",
    "clicks_unique",
    "delivered",
    "bounced",
    "unsubscribes",
    "spam_complaints",
    "recipients",
    "conversions",
    "conversion_value",
]

FLOW_STATS = [
    "opens",
    "opens_unique",
    "clicks",
    "clicks_unique",
    "delivered",
    "bounced",
    "unsubscribes",
    "recipients",
    "conversions",
    "conversion_value",
]

FORM_STATS = [
    # Klaviyo form-values stat names that the API accepts. Started broader
    # but trimmed after API rejections — re-expand cautiously if needed.
    "viewed_form",
    "submits",
    "qualified_form",
    "closed_form",
]


def fetch_values_report(report_type, statistics, since_iso, until_iso, conversion_metric_id=None):
    """POST to /<report_type>-values-reports/ for a single timeframe.

    Returns 'data.attributes.results' list. Each entry is one campaign /
    flow / form's stats aggregated over the timeframe.

    Note on rate limits: Klaviyo's burst limit on these endpoints is ~3-4
    rapid calls before triggering a ~55s cooldown. The retry handler eats
    that automatically. For the default 7-day lookback (21 calls total),
    expect 2-3 cooldowns, total runtime ~3-4 minutes. For wider backfills,
    runtime scales roughly linearly with lookback days.
    """
    url = f"{KLAVIYO_BASE}/{report_type}-values-reports/"
    attrs = {
        "statistics": statistics,
        "timeframe":  {"start": since_iso, "end": until_iso},
    }
    if conversion_metric_id:
        attrs["conversion_metric_id"] = conversion_metric_id
    body = {"data": {"type": f"{report_type}-values-report", "attributes": attrs}}
    resp = request_with_retry("POST", url, json_body=body)
    return resp.json().get("data", {}).get("attributes", {}).get("results", [])


def _fetch_daily(report_type, statistics, since, until, conversion_metric_id=None):
    """Loop one values-reports call per day across the window."""
    rows = []
    day = since
    while day <= until:
        start_iso = f"{day.isoformat()}T00:00:00Z"
        end_iso   = f"{day.isoformat()}T23:59:59Z"
        results = fetch_values_report(
            report_type, statistics, start_iso, end_iso, conversion_metric_id
        )
        for r in results:
            rows.append((day, r))
        day += timedelta(days=1)
    return rows


def fetch_campaigns_daily(since, until, conversion_metric_id):
    logger.info(f"Report 1/3: campaigns ({since} -> {until})")
    rows = _fetch_daily("campaign", CAMPAIGN_STATS, since, until, conversion_metric_id)
    logger.info(f"  campaigns row-days: {len(rows)}")
    return rows


def fetch_flows_daily(since, until, conversion_metric_id):
    logger.info(f"Report 2/3: flows ({since} -> {until})")
    rows = _fetch_daily("flow", FLOW_STATS, since, until, conversion_metric_id)
    logger.info(f"  flows row-days: {len(rows)}")
    return rows


def fetch_forms_daily(since, until):
    logger.info(f"Report 3/3: forms ({since} -> {until})")
    rows = _fetch_daily("form", FORM_STATS, since, until, conversion_metric_id=None)
    logger.info(f"  forms row-days: {len(rows)}")
    return rows

# ─── ROW FORMATTERS ───────────────────────────────────────────────────────────

def stat(result, name):
    """Pull a named statistic out of a values-report result dict."""
    stats = result.get("statistics") or {}
    return stats.get(name)


def format_campaign_row(day, result, now_iso, campaign_names=None):
    """Build a dict for email_campaigns (campaign_type='campaign')."""
    group = result.get("groupings", {}) or {}
    campaign_id = group.get("campaign_id") or group.get("send_channel") or "(unknown)"
    name = (campaign_names or {}).get(campaign_id) or group.get("campaign_name")
    return {
        "date":              day,
        "brand_id":          BRAND_ID,
        "campaign_id":       campaign_id,
        "campaign_name":     name,
        "campaign_type":     "campaign",
        "flow_id":           None,
        "flow_name":         None,
        "emails_sent":       safe_int(stat(result, "recipients")),
        "emails_delivered":  safe_int(stat(result, "delivered")),
        "unique_opens":      safe_int(stat(result, "opens_unique")),
        "unique_clicks":     safe_int(stat(result, "clicks_unique")),
        "revenue_attributed": safe_float(stat(result, "conversion_value")),
        "unsubscribes":      safe_int(stat(result, "unsubscribes")),
        "bounce_rate":       None,
        "metric_status":     "updating",
        "synced_at":         now_iso,
    }


def format_flow_row(day, result, now_iso, flow_names=None):
    """Build a dict for email_campaigns (campaign_type='flow').

    Klaviyo flow-values reports group by (flow_id, flow_message_id) — each
    flow has multiple message steps (e.g. Welcome 1 → Welcome 2 → Welcome 3).
    To preserve per-message detail without colliding on our UNIQUE key, we
    use a composite campaign_id of '<flow_id>:<flow_message_id>'. flow_id
    stays in its own column for easy grouping back to the parent flow.
    """
    group     = result.get("groupings", {}) or {}
    flow_id   = group.get("flow_id") or "(unknown)"
    flow_name = (flow_names or {}).get(flow_id) or group.get("flow_name")
    flow_msg  = group.get("flow_message_id")
    composite = f"{flow_id}:{flow_msg}" if flow_msg else flow_id
    return {
        "date":              day,
        "brand_id":          BRAND_ID,
        "campaign_id":       composite,
        "campaign_name":     flow_name,
        "campaign_type":     "flow",
        "flow_id":           flow_id,
        "flow_name":         flow_name,
        "emails_sent":       safe_int(stat(result, "recipients")),
        "emails_delivered":  safe_int(stat(result, "delivered")),
        "unique_opens":      safe_int(stat(result, "opens_unique")),
        "unique_clicks":     safe_int(stat(result, "clicks_unique")),
        "revenue_attributed": safe_float(stat(result, "conversion_value")),
        "unsubscribes":      safe_int(stat(result, "unsubscribes")),
        "bounce_rate":       None,
        "metric_status":     "updating",
        "synced_at":         now_iso,
    }


def format_form_row(day, result, now_iso, form_names=None):
    """Build a dict for klaviyo_forms_daily.

    Note: Klaviyo's form-values-reports doesn't expose revenue (no
    conversion_value stat for forms). We leave revenue_attributed=0 and
    note in the spec that form revenue is a future UTM-based join.
    """
    group   = result.get("groupings", {}) or {}
    form_id = group.get("form_id") or "(unknown)"
    name    = (form_names or {}).get(form_id) or group.get("form_name")
    return {
        "date":               day,
        "brand_id":           BRAND_ID,
        "form_id":            form_id,
        "form_name":          name,
        "views":              safe_int(stat(result, "viewed_form")),
        "submits":            safe_int(stat(result, "submits")),
        "qualified_submits":  safe_int(stat(result, "qualified_form")),
        "revenue_attributed": 0.0,
        "synced_at":          now_iso,
    }

# ─── UPSERTS ──────────────────────────────────────────────────────────────────

EMAIL_CAMPAIGNS_UPSERT = """
INSERT INTO email_campaigns (
    date, brand_id, campaign_id, campaign_name, campaign_type,
    flow_id, flow_name,
    emails_sent, emails_delivered, unique_opens, unique_clicks,
    revenue_attributed, unsubscribes, bounce_rate, metric_status, synced_at
) VALUES (
    %(date)s, %(brand_id)s, %(campaign_id)s, %(campaign_name)s, %(campaign_type)s,
    %(flow_id)s, %(flow_name)s,
    %(emails_sent)s, %(emails_delivered)s, %(unique_opens)s, %(unique_clicks)s,
    %(revenue_attributed)s, %(unsubscribes)s, %(bounce_rate)s, %(metric_status)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, campaign_id) DO UPDATE SET
    campaign_name      = EXCLUDED.campaign_name,
    campaign_type      = EXCLUDED.campaign_type,
    flow_id            = EXCLUDED.flow_id,
    flow_name          = EXCLUDED.flow_name,
    emails_sent        = EXCLUDED.emails_sent,
    emails_delivered   = EXCLUDED.emails_delivered,
    unique_opens       = EXCLUDED.unique_opens,
    unique_clicks      = EXCLUDED.unique_clicks,
    revenue_attributed = EXCLUDED.revenue_attributed,
    unsubscribes       = EXCLUDED.unsubscribes,
    bounce_rate        = EXCLUDED.bounce_rate,
    metric_status      = EXCLUDED.metric_status,
    synced_at          = EXCLUDED.synced_at
"""

KLAVIYO_FORMS_UPSERT = """
INSERT INTO klaviyo_forms_daily (
    date, brand_id, form_id, form_name,
    views, submits, qualified_submits, revenue_attributed, synced_at
) VALUES (
    %(date)s, %(brand_id)s, %(form_id)s, %(form_name)s,
    %(views)s, %(submits)s, %(qualified_submits)s, %(revenue_attributed)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, form_id) DO UPDATE SET
    form_name          = EXCLUDED.form_name,
    views              = EXCLUDED.views,
    submits            = EXCLUDED.submits,
    qualified_submits  = EXCLUDED.qualified_submits,
    revenue_attributed = EXCLUDED.revenue_attributed,
    synced_at          = EXCLUDED.synced_at
"""


def write_rows(conn, sql, rows, label):
    upserted = errors = 0
    with conn.cursor() as cur:
        for r in rows:
            try:
                cur.execute(sql, r)
                upserted += 1
            except Exception as e:
                conn.rollback()
                logger.error(f"{label} upsert failed for row {r}: {e}")
                errors += 1
                continue
    conn.commit()
    logger.info(f"Upserted {upserted} rows into {label} (errors: {errors})")
    return upserted, errors

# ─── ORCHESTRATION ────────────────────────────────────────────────────────────

def run_sync(lookback_days, dry_run=False):
    if not KLAVIYO_API_KEY:
        logger.critical("KLAVIYO_API_KEY missing from .env")
        sys.exit(1)
    test_db_connection()

    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=lookback_days)
    until = today

    logger.info(f"Klaviyo sync starting — {since} -> {until} ({lookback_days}-day lookback)")

    conversion_metric_id = get_conversion_metric_id()
    logger.info(f"Conversion metric (Placed Order) id: {conversion_metric_id}")

    # Pre-fetch id->name lookups (3 list calls). Without these, campaign_name
    # and flow_name come back empty because values-reports only returns IDs.
    logger.info("Pre-fetching id->name lookups for campaigns / flows / forms")
    campaign_names = fetch_campaign_names()
    flow_names     = fetch_flow_names()
    form_names     = fetch_form_names()

    campaign_rows = fetch_campaigns_daily(since, until, conversion_metric_id)
    flow_rows     = fetch_flows_daily(since, until, conversion_metric_id)
    form_rows     = fetch_forms_daily(since, until)

    if dry_run:
        logger.info(
            f"[DRY RUN] Would upsert campaigns={len(campaign_rows)} "
            f"flows={len(flow_rows)} forms={len(form_rows)}. No DB writes."
        )
        for label, rows in [("campaigns", campaign_rows), ("flows", flow_rows), ("forms", form_rows)]:
            if rows:
                d, r = rows[0]
                logger.info(f"  Sample {label}: date={d} groupings={r.get('groupings')} statistics={r.get('statistics')}")
        return 0, 0, 0, 0

    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        c_rows = [format_campaign_row(d, r, now_iso, campaign_names) for d, r in campaign_rows]
        c_up, c_err = write_rows(conn, EMAIL_CAMPAIGNS_UPSERT, c_rows, "email_campaigns (campaigns)")

        f_rows = [format_flow_row(d, r, now_iso, flow_names) for d, r in flow_rows]
        f_up, f_err = write_rows(conn, EMAIL_CAMPAIGNS_UPSERT, f_rows, "email_campaigns (flows)")

        fm_rows = [format_form_row(d, r, now_iso, form_names) for d, r in form_rows]
        fm_up, fm_err = write_rows(conn, KLAVIYO_FORMS_UPSERT, fm_rows, "klaviyo_forms_daily")

        total_errors = c_err + f_err + fm_err
    finally:
        conn.close()

    logger.info(
        f"Sync complete — campaigns:{c_up} flows:{f_up} forms:{fm_up} | errors:{total_errors}"
    )
    return c_up, f_up, fm_up, total_errors

# ─── SCHEDULED CAMPAIGNS ──────────────────────────────────────────────────────

def sync_scheduled_campaigns(dry_run=False):
    """
    Pull every scheduled/draft email campaign from the Klaviyo Campaigns API
    and upsert into klaviyo_scheduled_campaigns. Sweeps rows whose campaigns
    are no longer in scheduled/draft state (sent or cancelled since last
    sync) so the table is always a live "what's coming up" snapshot.

    Endpoint: GET /api/campaigns/  with revision header 2024-02-15.
    Filter:   server-side equals(messages.channel,'email'); status filtered
              client-side because Klaviyo doesn't expose status in the
              filter grammar reliably across revisions.
    """
    if not KLAVIYO_API_KEY:
        logger.error("sync_scheduled_campaigns: KLAVIYO_API_KEY missing")
        return 0

    logger.info("Klaviyo scheduled campaigns: starting")
    url    = "https://a.klaviyo.com/api/campaigns/"
    params = {"filter": "equals(messages.channel,'email')"}

    rows      = []
    seen_ids  = set()
    page      = 0
    while url:
        page += 1
        resp = request_with_retry("GET", url, params=params)
        body = resp.json()
        data = body.get("data", []) or []
        for c in data:
            attrs      = c.get("attributes") or {}
            status_raw = attrs.get("status") or ""
            # Klaviyo's actual enum (rev 2024-02-15): "Draft", "Sent",
            # "Queued without Recipients", "Cancelled", "Sending". The
            # forward-looking states we care about are "Draft" (informational)
            # and "Queued without Recipients" (a campaign user-scheduled for
            # the future) — the latter is what users colloquially call
            # "scheduled", so we normalise it to that on the way into the
            # table so the priority-view filter (WHERE status='scheduled')
            # works as the v8.25 brief expected.
            sr = status_raw.lower()
            if sr == "draft":
                status = "draft"
            elif "queued" in sr:
                status = "scheduled"
            else:
                continue

            # Real future send time lives at attributes.send_time (also at
            # send_strategy.options_static.datetime for the 'static' method).
            # attributes.scheduled_at is the QUEUE-creation time, not the
            # send time — using it would put every "Sent on June 11" row at
            # "May 13" in our table. Use send_time instead.
            send_strategy = attrs.get("send_strategy") or {}
            options_static = (send_strategy.get("options_static") or {})
            scheduled_at = (
                attrs.get("send_time")
                or options_static.get("datetime")
            )

            # "Send in recipient's local timezone" lives on options_static
            # for static-scheduled campaigns; brief's send_options.use_smart_sending
            # is a different feature entirely (smart-send-time AI).
            is_local = options_static.get("is_local")
            if is_local is None and send_strategy.get("method") != "static":
                is_local = None  # not applicable for non-static schedules

            campaign_id = c.get("id")
            rows.append({
                "campaign_id":        campaign_id,
                "campaign_name":      attrs.get("name"),
                "status":             status,
                "scheduled_at":       scheduled_at,
                "send_time_is_local": is_local,
            })
            seen_ids.add(campaign_id)
        logger.info(f"  page {page}: {len(data)} fetched, {len(rows)} matching status so far")

        next_link = (body.get("links") or {}).get("next")
        if not next_link:
            break
        url    = next_link
        params = None  # next URL already carries params

    logger.info(f"  total scheduled/draft campaigns: {len(rows)}")

    if dry_run:
        for r in rows:
            logger.info(f"  (dry-run) {r['campaign_id']:>18} {r['status']:<10} "
                        f"{(r['scheduled_at'] or '-'):<28} {r['campaign_name']}")
        return len(rows)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for r in rows:
            cur.execute(
                """
                INSERT INTO klaviyo_scheduled_campaigns (
                    brand_id, campaign_id, campaign_name, status,
                    scheduled_at, send_time_is_local, synced_at
                ) VALUES ('your_brand_id', %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (campaign_id, brand_id) DO UPDATE SET
                    campaign_name      = EXCLUDED.campaign_name,
                    status             = EXCLUDED.status,
                    scheduled_at       = EXCLUDED.scheduled_at,
                    send_time_is_local = EXCLUDED.send_time_is_local,
                    synced_at          = NOW();
                """,
                (r["campaign_id"], r["campaign_name"], r["status"],
                 r["scheduled_at"], r["send_time_is_local"]),
            )
        # Sweep stale rows — anything not in the current scheduled/draft list
        # has been sent, cancelled, or otherwise left the upcoming queue.
        cur.execute(
            """
            DELETE FROM klaviyo_scheduled_campaigns
            WHERE brand_id = 'your_brand_id'
              AND NOT (campaign_id = ANY(%s));
            """,
            (list(seen_ids),),
        )
        swept = cur.rowcount
        conn.commit()
        cur.close()
        logger.info(f"  upserted {len(rows)}, swept {swept} stale row(s)")
    finally:
        conn.close()

    return len(rows)

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Klaviyo → Postgres sync")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK,
                        help=f"Days to look back (default {DEFAULT_LOOKBACK})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + summarise without writing to DB")
    parser.add_argument("--scheduled-only", action="store_true",
                        help="Skip the stats sync, only refresh klaviyo_scheduled_campaigns")
    args = parser.parse_args()

    if args.scheduled_only:
        sync_scheduled_campaigns(dry_run=args.dry_run)
        logger.info("Script complete (scheduled-only)")
        sys.exit(0)

    c, f, fm, errors = run_sync(args.lookback_days, dry_run=args.dry_run)
    # Always refresh upcoming campaigns alongside the daily stats sweep
    if not args.dry_run:
        try:
            sync_scheduled_campaigns()
        except Exception as e:
            logger.error(f"sync_scheduled_campaigns failed (non-fatal): {e}")
            errors += 1
    logger.info("Script complete")
    sys.exit(1 if errors > 0 else 0)
