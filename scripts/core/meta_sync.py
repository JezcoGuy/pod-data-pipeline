"""
meta_sync.py
============
Syncs Meta Marketing API data to PostgreSQL.
Pure data ingestion — no alert logic.

Architecture (three phases per run):

  1. fetch_ad_metadata
       GET /act_X/ads with creative expansion. Builds an ad_id -> {
       destination_url, destination_type, product_set_id, ad_status } map.
       Used to populate metadata on ad_campaigns rows AND classify ads as
       catalogue (has product_set_id) vs static.

  2. fetch_ad_day_insights
       GET /act_X/insights at level=ad with time_increment=1 across the
       lookback window. One row per ad per day with all major fields,
       upserted into ad_campaigns. raw_payload JSONB captures the whole
       response for future schema extension without re-syncing.

  3. fetch_catalogue_breakdown
       For each catalogue ad x each day, GET /insights with
       breakdowns=product_id, filtered to that single ad and single day.
       Tight chunking avoids the 500 'response too large' error we saw on
       wider queries. Each row resolved via product_catalogue.variant_id
       and upserted into ad_campaign_products.

Static ads do not write to ad_campaign_products — their attribution is via
orders.utm_* joined to ad_campaigns in a downstream Metabase view.

Usage:
    python3 meta_sync.py                          # last 7 days
    python3 meta_sync.py --lookback-days 90       # backfill / wider window
    python3 meta_sync.py --single-ad 1202410...   # debug one ad
    python3 meta_sync.py --dry-run                # fetch only, no DB writes
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import requests
import psycopg2
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv("/opt/your_brand_id/.env")

META_ACCESS_TOKEN   = os.getenv("META_ACCESS_TOKEN")
META_AD_ACCOUNT_ID  = (os.getenv("META_AD_ACCOUNT_ID", "")
                        .lstrip("act_").strip())
META_API_VERSION    = os.getenv("META_API_VERSION", "v21.0")
META_BASE           = f"https://graph.facebook.com/{META_API_VERSION}"

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

BRAND_ID         = os.getenv("BRAND_ID", "your_brand_id")
REQUEST_TIMEOUT  = int(os.getenv("REQUEST_TIMEOUT", "30"))
DEFAULT_LOOKBACK = int(os.getenv("META_LOOKBACK_DAYS", "7"))
LOG_FILE         = os.getenv("META_LOG_FILE", "logs/meta_sync.log")

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)

logger = logging.getLogger("meta_sync")
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

# ─── POSTGRES ─────────────────────────────────────────────────────────────────

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

# ─── META API ─────────────────────────────────────────────────────────────────

INSIGHTS_FIELDS = ",".join([
    # Hierarchy + metadata
    "account_id", "account_currency", "account_name",
    "campaign_id", "campaign_name",
    "adset_id", "adset_name",
    "ad_id", "ad_name",
    "objective", "attribution_setting",
    "date_start", "date_stop",
    # Engagement
    "spend", "impressions", "reach", "frequency",
    "clicks", "inline_link_clicks", "outbound_clicks", "unique_clicks",
    # Conversion actions / values arrays (we parse out omni_* variants below)
    "actions", "action_values", "purchase_roas",
    # Video
    "video_play_actions",
    "video_p25_watched_actions", "video_p50_watched_actions",
    "video_p75_watched_actions", "video_p95_watched_actions",
    "video_p100_watched_actions",
    "video_avg_time_watched_actions",
    # Rankings
    "quality_ranking", "engagement_rate_ranking", "conversion_rate_ranking",
])

BREAKDOWN_FIELDS = ",".join([
    "ad_id", "campaign_id", "adset_id",
    "spend", "impressions", "clicks", "inline_link_clicks",
    "actions", "action_values",
    "date_start", "date_stop",
])

AD_META_FIELDS = (
    "id,name,status,effective_status,campaign_id,adset_id,updated_time,"
    "creative{id,object_story_spec,link_url,product_set_id,object_type}"
)


def request_with_retry(url, params=None, max_retries=6):
    """GET with rate limit handling, exponential backoff and 5xx retry.

    Two Meta-specific quirks handled here:
      1. Pagination 'next' URLs already contain access_token. Re-adding it via
         params causes token doubling (URL grows each page). We detect this
         and skip the re-add.
      2. App rate limit comes back as 403 + error_subcode 1504022, not 429.
         Needs much longer waits (Meta's 'ad load score' window is ~5 minutes).
    """
    backoff = 2
    last_error = None
    for attempt in range(max_retries):
        try:
            full_params = dict(params or {})
            # Don't add access_token if the URL already has one (Meta's next URL).
            if "access_token=" not in url:
                full_params["access_token"] = META_ACCESS_TOKEN
            resp = requests.get(url, params=full_params, timeout=REQUEST_TIMEOUT)

            # App rate limit — Meta uses 403 + specific subcode for this.
            if resp.status_code == 403:
                try:
                    err = resp.json().get("error", {}) or {}
                except ValueError:
                    err = {}
                if err.get("code") == 4 or err.get("error_subcode") == 1504022:
                    wait = max(60, min(300 * (attempt + 1), 600))
                    logger.warning(
                        f"App rate limit hit (attempt {attempt+1}/{max_retries}) — "
                        f"sleeping {wait}s. Meta says: "
                        f"{err.get('error_user_msg', '')[:120]}"
                    )
                    time.sleep(wait)
                    continue
                # other 403 — fall through to error path

            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", backoff))
                logger.warning(f"429 rate limited — retry in {wait}s")
                time.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue

            if resp.status_code >= 500:
                logger.warning(
                    f"Meta {resp.status_code} (attempt {attempt+1}) — "
                    f"retry in {backoff}s. Body: {resp.text[:200]}"
                )
                last_error = resp.text
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            if resp.status_code != 200:
                logger.error(f"Meta {resp.status_code}: {resp.text[:400]}")
                resp.raise_for_status()

            # Tiny pacing between successful calls
            time.sleep(0.25)
            return resp
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Connection error (attempt {attempt+1}): {e}")
            last_error = str(e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
    raise Exception(f"Failed after {max_retries} attempts: {url} (last: {last_error})")


def paginate(url, params):
    """Generator: yields each row across paginated Meta responses."""
    next_url = url
    next_params = params
    while next_url:
        resp = request_with_retry(next_url, params=next_params)
        data = resp.json()
        for row in data.get("data", []):
            yield row
        paging = data.get("paging") or {}
        next_url = paging.get("next")
        next_params = None  # next URL has params baked in

# ─── PARSING HELPERS ──────────────────────────────────────────────────────────

def safe_int(v, default=0):
    if v is None or v == "":
        return default
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def safe_float(v, default=0.0):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def extract_action(actions, action_type):
    """Look up action_type in Meta's actions[] array. Return integer count."""
    if not actions:
        return 0
    for a in actions:
        if a.get("action_type") == action_type:
            return safe_int(a.get("value"))
    return 0


def extract_value(values, action_type):
    """Look up action_type in Meta's action_values[] array. Return float GBP."""
    if not values:
        return 0.0
    for v in values:
        if v.get("action_type") == action_type:
            return safe_float(v.get("value"))
    return 0.0


def extract_first_action_value(arr, as_float=False):
    """Meta video quartile arrays come back as [{action_type, value}].
    Return the value of the first element (typically only one entry)."""
    if not arr or not isinstance(arr, list):
        return 0.0 if as_float else 0
    first = arr[0]
    return safe_float(first.get("value")) if as_float else safe_int(first.get("value"))


def extract_destination_url(creative):
    """Pull the destination URL out of an ad creative.
    Returns None for catalogue/dynamic ads (no single URL)."""
    if not creative:
        return None
    spec = (creative.get("object_story_spec") or {}).get("link_data") or {}
    link = spec.get("link")
    if link:
        return link
    return creative.get("link_url")


def classify_destination(url, has_product_set):
    """Classify the destination URL for reporting/grouping.
    Catalogue ads -> 'dynamic'. Static -> product/collection/homepage/other."""
    if has_product_set:
        return "dynamic"
    if not url:
        return None
    path = urlparse(url).path.lower()
    if "/products/" in path:
        return "product"
    if "/collections/" in path:
        return "collection"
    if path in ("", "/"):
        return "homepage"
    return "other"


def is_catalogue_ad(creative):
    return bool((creative or {}).get("product_set_id"))

# ─── PHASE 1: AD METADATA ─────────────────────────────────────────────────────

def fetch_ad_metadata(single_ad_id=None):
    """Return dict ad_id -> {destination_url, destination_type, ad_status,
    is_catalogue, product_set_id, creative_id}."""
    logger.info("Phase 1: fetching ad metadata via /ads")
    url = f"{META_BASE}/act_{META_AD_ACCOUNT_ID}/ads"
    params = {
        "fields": AD_META_FIELDS,
        "limit": 100,
        # Default Meta behaviour returns ACTIVE+PAUSED. For backfill of older
        # ads, callers can re-run with effective_status filter widened later.
    }
    if single_ad_id:
        params["filtering"] = json.dumps([
            {"field": "ad.id", "operator": "IN", "value": [single_ad_id]},
        ])

    ad_map = {}
    for ad in paginate(url, params):
        creative = ad.get("creative") or {}
        is_cat = is_catalogue_ad(creative)
        url_ = extract_destination_url(creative)
        ad_map[ad["id"]] = {
            "is_catalogue":     is_cat,
            "destination_url":  url_,
            "destination_type": classify_destination(url_, is_cat),
            "ad_status":        ad.get("effective_status"),
            "product_set_id":   creative.get("product_set_id"),
            "creative_id":      creative.get("id"),
        }

    catalogue_count = sum(1 for v in ad_map.values() if v["is_catalogue"])
    static_count    = len(ad_map) - catalogue_count
    logger.info(f"  fetched {len(ad_map)} ads ({catalogue_count} catalogue, {static_count} static)")
    return ad_map

# ─── PHASE 2: AD-DAY INSIGHTS ─────────────────────────────────────────────────

def fetch_ad_day_insights(lookback_days, single_ad_id=None, chunk_days=7):
    """One row per ad per day for the lookback window.

    Chunks the time range into smaller windows (default 7 days each) to avoid
    ReadTimeout. Meta's /insights call for a 90-day window doesn't return
    within our 30s timeout; per-week chunks complete in seconds each.
    """
    logger.info(
        f"Phase 2: fetching ad-day insights ({lookback_days}d window, "
        f"{chunk_days}-day chunks)"
    )
    today = datetime.now(timezone.utc).date()
    overall_since = today - timedelta(days=lookback_days)

    # Build inclusive [since, until] chunks of <= chunk_days each.
    chunks = []
    chunk_start = overall_since
    while chunk_start <= today:
        chunk_end = min(chunk_start + timedelta(days=chunk_days - 1), today)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)

    url = f"{META_BASE}/act_{META_AD_ACCOUNT_ID}/insights"
    rows = []

    for chunk_since, chunk_until in chunks:
        params = {
            "fields":         INSIGHTS_FIELDS,
            "level":          "ad",
            "time_range":     json.dumps({
                "since": chunk_since.isoformat(),
                "until": chunk_until.isoformat(),
            }),
            "time_increment": 1,
            "limit":          100,
        }
        if single_ad_id:
            params["filtering"] = json.dumps([
                {"field": "ad.id", "operator": "IN", "value": [single_ad_id]},
            ])
        chunk_rows = list(paginate(url, params))
        rows.extend(chunk_rows)
        logger.debug(
            f"  chunk {chunk_since}..{chunk_until}: {len(chunk_rows)} rows"
        )

    logger.info(f"  fetched {len(rows)} ad-day rows across {len(chunks)} chunks")
    return rows

# ─── PHASE 3: CATALOGUE BREAKDOWN ─────────────────────────────────────────────

def fetch_catalogue_breakdown(ad_day_pairs):
    """Fetch product_id breakdown for specific (ad_id, date_str) tuples.

    Only call this for ad-days that we know had spend (Phase 2 confirms it).
    Otherwise we waste an API round-trip per zero-spend day, which is the
    difference between a 1-minute sync and a 20-minute sync.
    """
    logger.info(f"Phase 3: fetching catalogue breakdown ({len(ad_day_pairs)} ad-day calls)")
    if not ad_day_pairs:
        logger.info("  no catalogue ad-days with spend — skipping")
        return []

    url = f"{META_BASE}/act_{META_AD_ACCOUNT_ID}/insights"
    rows = []
    errors = 0

    for ad_id, date_str in ad_day_pairs:
        params = {
            "fields":         BREAKDOWN_FIELDS,
            "level":          "ad",
            "time_range":     json.dumps({"since": date_str, "until": date_str}),
            "time_increment": 1,
            "breakdowns":     "product_id",
            "filtering":      json.dumps([
                {"field": "ad.id", "operator": "IN", "value": [ad_id]},
            ]),
            "limit":          500,
        }
        try:
            resp = request_with_retry(url, params=params)
            batch = resp.json().get("data", [])
            for r in batch:
                r.setdefault("ad_id", ad_id)
                r.setdefault("date_start", date_str)
            rows.extend(batch)
        except Exception as e:
            logger.error(f"  breakdown failed for ad={ad_id} day={date_str}: {e}")
            errors += 1
            continue

    logger.info(f"  fetched {len(rows)} breakdown rows ({errors} errors)")
    return rows


def build_catalogue_ad_days(ad_day_rows, catalogue_ad_ids, single_ad_id=None):
    """Filter Phase 2 results to (ad_id, date) tuples worth calling breakdown for.

    A tuple is included only if the ad is in the catalogue set AND had non-zero
    spend on that day. This is what saves us from doing 1000+ calls when most
    catalogue ads are paused / didn't spend in the window.
    """
    catalogue_set = set(catalogue_ad_ids)
    pairs = []
    for row in ad_day_rows:
        aid = row.get("ad_id")
        if not aid or aid not in catalogue_set:
            continue
        if single_ad_id and aid != single_ad_id:
            continue
        if safe_float(row.get("spend")) <= 0:
            continue
        date_str = row.get("date_start")
        if date_str:
            pairs.append((aid, date_str))
    return pairs

# ─── ROW FORMATTERS ───────────────────────────────────────────────────────────

def format_ad_campaign_row(insight, ad_meta, now_iso):
    """Build a dict matching ad_campaigns columns from a Phase 2 insight row."""
    actions = insight.get("actions") or []
    values  = insight.get("action_values") or []
    meta    = ad_meta.get(insight.get("ad_id", "")) or {}

    outbound = insight.get("outbound_clicks") or []
    outbound_count = (
        safe_int(outbound[0].get("value")) if isinstance(outbound, list) and outbound else 0
    )

    return {
        "date":              insight.get("date_start"),
        "brand_id":          BRAND_ID,
        "platform":          "meta",
        "campaign_id":       insight.get("campaign_id"),
        "campaign_name":     insight.get("campaign_name"),
        "adset_id":          insight.get("adset_id"),
        "adset_name":        insight.get("adset_name"),
        "ad_id":             insight.get("ad_id"),
        "ad_name":           insight.get("ad_name"),

        "account_id":        insight.get("account_id"),
        "account_currency":  insight.get("account_currency"),
        "account_name":      insight.get("account_name"),
        "objective":         insight.get("objective"),
        "ad_status":         meta.get("ad_status"),
        "attribution_setting": insight.get("attribution_setting"),
        "destination_url":   meta.get("destination_url"),
        "destination_type":  meta.get("destination_type"),

        "spend_gbp":         safe_float(insight.get("spend")),
        "impressions":       safe_int(insight.get("impressions")),
        "reach":             safe_int(insight.get("reach")),
        "frequency":         safe_float(insight.get("frequency")),
        "clicks":            safe_int(insight.get("clicks")),
        "link_clicks":       safe_int(insight.get("inline_link_clicks")),
        "outbound_clicks":   outbound_count,
        "unique_clicks":     safe_int(insight.get("unique_clicks")),

        "video_plays":         extract_first_action_value(insight.get("video_play_actions")),
        "video_plays_25_pct":  extract_first_action_value(insight.get("video_p25_watched_actions")),
        "video_plays_50_pct":  extract_first_action_value(insight.get("video_p50_watched_actions")),
        "video_plays_75_pct":  extract_first_action_value(insight.get("video_p75_watched_actions")),
        "video_plays_95_pct":  extract_first_action_value(insight.get("video_p95_watched_actions")),
        "video_plays_100_pct": extract_first_action_value(insight.get("video_p100_watched_actions")),
        "video_avg_time_watched_sec":
            extract_first_action_value(insight.get("video_avg_time_watched_actions"), as_float=True),

        "landing_page_views":
            extract_action(actions, "omni_landing_page_view")
            or extract_action(actions, "landing_page_view"),

        "view_content_count":         extract_action(actions, "omni_view_content"),
        "view_content_value_gbp":     extract_value(values, "omni_view_content"),
        "add_to_cart_count":          extract_action(actions, "omni_add_to_cart"),
        "add_to_cart_value_gbp":      extract_value(values, "omni_add_to_cart"),
        "initiate_checkout_count":    extract_action(actions, "omni_initiated_checkout"),
        "initiate_checkout_value_gbp": extract_value(values, "omni_initiated_checkout"),
        "add_payment_info_count":     extract_action(actions, "add_payment_info"),

        "meta_reported_purchases":    extract_action(actions, "omni_purchase"),
        "meta_reported_revenue":      extract_value(values, "omni_purchase"),

        "quality_ranking":            insight.get("quality_ranking"),
        "engagement_rate_ranking":    insight.get("engagement_rate_ranking"),
        "conversion_rate_ranking":    insight.get("conversion_rate_ranking"),

        "raw_payload":                json.dumps(insight),
        "synced_at":                  now_iso,
    }


def build_variant_resolver_map(cur, meta_product_ids):
    """Resolve a batch of compound meta_product_ids in ONE SELECT.

    Replaces the per-row resolve_variant() pattern. For a 90-day backfill
    with ~286k breakdown rows, this drops ~286k SELECTs against
    product_catalogue down to a single bulk query — the lookup during the
    write loop is then a plain dict access.

    Returns: dict mapping each meta_product_id (compound string from Meta)
    to its resolved row dict (same shape resolve_variant used to return).
    """
    parsed = {}
    distinct_variant_ids = set()
    for pid in meta_product_ids:
        variant_id, _, title_from_meta = pid.partition(",")
        variant_id = variant_id.strip()
        title_from_meta = title_from_meta.strip() or None
        parsed[pid] = (variant_id, title_from_meta)
        if variant_id:
            distinct_variant_ids.add(variant_id)

    variant_lookups = {}
    if distinct_variant_ids:
        cur.execute("""
            SELECT variant_id, product_id, product_title, variant_title, product_handle
            FROM product_catalogue
            WHERE brand_id = %s AND variant_id = ANY(%s)
        """, (BRAND_ID, list(distinct_variant_ids)))
        for variant_id, product_id, product_title, variant_title, product_handle in cur.fetchall():
            variant_lookups[variant_id] = (product_id, product_title, variant_title, product_handle)

    resolver = {}
    for pid, (variant_id, title_from_meta) in parsed.items():
        match = variant_lookups.get(variant_id)
        if match:
            shopify_product_id, db_title, variant_title, product_handle = match
            resolver[pid] = {
                "meta_variant_id":    variant_id,
                "shopify_product_id": shopify_product_id,
                "shopify_variant_id": variant_id,
                "product_title":      db_title or title_from_meta,
                "variant_title":      variant_title,
                "product_handle":     product_handle,
                "attribution_source": "meta_catalog",
                "match_method":       "variant_id_exact",
            }
        else:
            resolver[pid] = {
                "meta_variant_id":    variant_id,
                "shopify_product_id": None,
                "shopify_variant_id": None,
                "product_title":      title_from_meta,
                "variant_title":      None,
                "product_handle":     None,
                "attribution_source": "unresolved",
                "match_method":       None,
            }
    return resolver


def format_ad_campaign_product_row(breakdown, resolver_map, now_iso):
    """Build a dict for ad_campaign_products from a Phase 3 breakdown row.

    Looks up variant resolution from resolver_map (pre-built by
    build_variant_resolver_map) — no DB hits during the write loop.
    """
    actions = breakdown.get("actions") or []
    values  = breakdown.get("action_values") or []
    resolved = resolver_map[breakdown["product_id"]]

    return {
        "date":                breakdown.get("date_start"),
        "brand_id":            BRAND_ID,
        "platform":            "meta",
        "ad_id":               breakdown.get("ad_id"),
        "campaign_id":         breakdown.get("campaign_id"),
        "adset_id":            breakdown.get("adset_id"),
        "meta_product_id":     breakdown["product_id"],
        **resolved,
        "spend_gbp":              safe_float(breakdown.get("spend")),
        "impressions":            safe_int(breakdown.get("impressions")),
        "clicks":                 safe_int(breakdown.get("clicks")),
        "link_clicks":            safe_int(breakdown.get("inline_link_clicks")),
        "meta_reported_purchases": extract_action(actions, "omni_purchase"),
        "meta_reported_revenue":  extract_value(values, "omni_purchase"),
        "synced_at":              now_iso,
    }

# ─── UPSERTS ──────────────────────────────────────────────────────────────────

AD_CAMPAIGNS_UPSERT = """
INSERT INTO ad_campaigns (
    date, brand_id, platform,
    campaign_id, campaign_name, adset_id, adset_name, ad_id, ad_name,
    account_id, account_currency, account_name,
    objective, ad_status, attribution_setting,
    destination_url, destination_type,
    spend_gbp, impressions, reach, frequency,
    clicks, link_clicks, outbound_clicks, unique_clicks,
    video_plays,
    video_plays_25_pct, video_plays_50_pct, video_plays_75_pct,
    video_plays_95_pct, video_plays_100_pct, video_avg_time_watched_sec,
    landing_page_views,
    view_content_count, view_content_value_gbp,
    add_to_cart_count, add_to_cart_value_gbp,
    initiate_checkout_count, initiate_checkout_value_gbp,
    add_payment_info_count,
    meta_reported_purchases, meta_reported_revenue,
    quality_ranking, engagement_rate_ranking, conversion_rate_ranking,
    raw_payload, synced_at
) VALUES (
    %(date)s, %(brand_id)s, %(platform)s,
    %(campaign_id)s, %(campaign_name)s, %(adset_id)s, %(adset_name)s, %(ad_id)s, %(ad_name)s,
    %(account_id)s, %(account_currency)s, %(account_name)s,
    %(objective)s, %(ad_status)s, %(attribution_setting)s,
    %(destination_url)s, %(destination_type)s,
    %(spend_gbp)s, %(impressions)s, %(reach)s, %(frequency)s,
    %(clicks)s, %(link_clicks)s, %(outbound_clicks)s, %(unique_clicks)s,
    %(video_plays)s,
    %(video_plays_25_pct)s, %(video_plays_50_pct)s, %(video_plays_75_pct)s,
    %(video_plays_95_pct)s, %(video_plays_100_pct)s, %(video_avg_time_watched_sec)s,
    %(landing_page_views)s,
    %(view_content_count)s, %(view_content_value_gbp)s,
    %(add_to_cart_count)s, %(add_to_cart_value_gbp)s,
    %(initiate_checkout_count)s, %(initiate_checkout_value_gbp)s,
    %(add_payment_info_count)s,
    %(meta_reported_purchases)s, %(meta_reported_revenue)s,
    %(quality_ranking)s, %(engagement_rate_ranking)s, %(conversion_rate_ranking)s,
    %(raw_payload)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, platform, ad_id) DO UPDATE SET
    campaign_id              = EXCLUDED.campaign_id,
    campaign_name            = EXCLUDED.campaign_name,
    adset_id                 = EXCLUDED.adset_id,
    adset_name               = EXCLUDED.adset_name,
    ad_name                  = EXCLUDED.ad_name,
    account_id               = EXCLUDED.account_id,
    account_currency         = EXCLUDED.account_currency,
    account_name             = EXCLUDED.account_name,
    objective                = EXCLUDED.objective,
    ad_status                = EXCLUDED.ad_status,
    attribution_setting      = EXCLUDED.attribution_setting,
    destination_url          = COALESCE(EXCLUDED.destination_url, ad_campaigns.destination_url),
    destination_type         = COALESCE(EXCLUDED.destination_type, ad_campaigns.destination_type),
    spend_gbp                = EXCLUDED.spend_gbp,
    impressions              = EXCLUDED.impressions,
    reach                    = EXCLUDED.reach,
    frequency                = EXCLUDED.frequency,
    clicks                   = EXCLUDED.clicks,
    link_clicks              = EXCLUDED.link_clicks,
    outbound_clicks          = EXCLUDED.outbound_clicks,
    unique_clicks            = EXCLUDED.unique_clicks,
    video_plays              = EXCLUDED.video_plays,
    video_plays_25_pct       = EXCLUDED.video_plays_25_pct,
    video_plays_50_pct       = EXCLUDED.video_plays_50_pct,
    video_plays_75_pct       = EXCLUDED.video_plays_75_pct,
    video_plays_95_pct       = EXCLUDED.video_plays_95_pct,
    video_plays_100_pct      = EXCLUDED.video_plays_100_pct,
    video_avg_time_watched_sec = EXCLUDED.video_avg_time_watched_sec,
    landing_page_views       = EXCLUDED.landing_page_views,
    view_content_count       = EXCLUDED.view_content_count,
    view_content_value_gbp   = EXCLUDED.view_content_value_gbp,
    add_to_cart_count        = EXCLUDED.add_to_cart_count,
    add_to_cart_value_gbp    = EXCLUDED.add_to_cart_value_gbp,
    initiate_checkout_count  = EXCLUDED.initiate_checkout_count,
    initiate_checkout_value_gbp = EXCLUDED.initiate_checkout_value_gbp,
    add_payment_info_count   = EXCLUDED.add_payment_info_count,
    meta_reported_purchases  = EXCLUDED.meta_reported_purchases,
    meta_reported_revenue    = EXCLUDED.meta_reported_revenue,
    quality_ranking          = EXCLUDED.quality_ranking,
    engagement_rate_ranking  = EXCLUDED.engagement_rate_ranking,
    conversion_rate_ranking  = EXCLUDED.conversion_rate_ranking,
    raw_payload              = EXCLUDED.raw_payload,
    synced_at                = EXCLUDED.synced_at
"""

AD_CAMPAIGN_PRODUCTS_UPSERT = """
INSERT INTO ad_campaign_products (
    date, brand_id, platform,
    ad_id, campaign_id, adset_id,
    meta_product_id, meta_variant_id,
    shopify_product_id, shopify_variant_id,
    product_title, variant_title, product_handle,
    attribution_source, match_method,
    spend_gbp, impressions, clicks, link_clicks,
    meta_reported_purchases, meta_reported_revenue,
    synced_at
) VALUES (
    %(date)s, %(brand_id)s, %(platform)s,
    %(ad_id)s, %(campaign_id)s, %(adset_id)s,
    %(meta_product_id)s, %(meta_variant_id)s,
    %(shopify_product_id)s, %(shopify_variant_id)s,
    %(product_title)s, %(variant_title)s, %(product_handle)s,
    %(attribution_source)s, %(match_method)s,
    %(spend_gbp)s, %(impressions)s, %(clicks)s, %(link_clicks)s,
    %(meta_reported_purchases)s, %(meta_reported_revenue)s,
    %(synced_at)s
)
ON CONFLICT (date, brand_id, platform, ad_id, meta_product_id) DO UPDATE SET
    campaign_id              = EXCLUDED.campaign_id,
    adset_id                 = EXCLUDED.adset_id,
    meta_variant_id          = EXCLUDED.meta_variant_id,
    shopify_product_id       = EXCLUDED.shopify_product_id,
    shopify_variant_id       = EXCLUDED.shopify_variant_id,
    product_title            = EXCLUDED.product_title,
    variant_title            = EXCLUDED.variant_title,
    product_handle           = EXCLUDED.product_handle,
    attribution_source       = EXCLUDED.attribution_source,
    match_method             = EXCLUDED.match_method,
    spend_gbp                = EXCLUDED.spend_gbp,
    impressions              = EXCLUDED.impressions,
    clicks                   = EXCLUDED.clicks,
    link_clicks              = EXCLUDED.link_clicks,
    meta_reported_purchases  = EXCLUDED.meta_reported_purchases,
    meta_reported_revenue    = EXCLUDED.meta_reported_revenue,
    synced_at                = EXCLUDED.synced_at
"""

# ─── ORCHESTRATION ────────────────────────────────────────────────────────────

def run_sync(lookback_days, single_ad_id=None, dry_run=False):
    if not META_ACCESS_TOKEN or not META_AD_ACCOUNT_ID:
        logger.critical("META_ACCESS_TOKEN or META_AD_ACCOUNT_ID missing from .env")
        sys.exit(1)

    test_db_connection()

    # Phase 1: ad metadata
    ad_meta = fetch_ad_metadata(single_ad_id=single_ad_id)
    catalogue_ad_ids = [aid for aid, m in ad_meta.items() if m["is_catalogue"]]

    # Phase 2: ad-day insights for all ads
    ad_day_rows = fetch_ad_day_insights(lookback_days, single_ad_id=single_ad_id)

    # Phase 3: catalogue breakdowns — only for (ad, day) tuples with real spend
    catalogue_ad_days = build_catalogue_ad_days(
        ad_day_rows, catalogue_ad_ids, single_ad_id=single_ad_id,
    )
    active_cat_ads = len({aid for aid, _ in catalogue_ad_days})
    logger.info(
        f"  Phase 3 target: {len(catalogue_ad_days)} ad-day calls "
        f"({active_cat_ads} active catalogue ads with spend, "
        f"out of {len(catalogue_ad_ids)} catalogue ads total)"
    )
    breakdown_rows = fetch_catalogue_breakdown(catalogue_ad_days)

    if dry_run:
        logger.info(
            f"[DRY RUN] Would upsert {len(ad_day_rows)} ad-day rows and "
            f"{len(breakdown_rows)} catalogue breakdown rows. No DB writes."
        )
        if ad_day_rows:
            logger.info(f"  Sample ad-day: {ad_day_rows[0].get('ad_name')} "
                        f"on {ad_day_rows[0].get('date_start')} "
                        f"spend=£{ad_day_rows[0].get('spend')}")
        if breakdown_rows:
            logger.info(f"  Sample breakdown: product_id={breakdown_rows[0]['product_id']}")
        return 0, 0, 0

    # Write to DB
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    upserted_ads = 0
    upserted_products = 0
    errors = 0

    try:
        with conn.cursor() as cur:
            # ad_campaigns
            for insight in ad_day_rows:
                try:
                    row = format_ad_campaign_row(insight, ad_meta, now_iso)
                    if not row["ad_id"] or not row["date"]:
                        logger.warning(f"Skipping malformed insight row: {insight.keys()}")
                        continue
                    cur.execute(AD_CAMPAIGNS_UPSERT, row)
                    upserted_ads += 1
                except Exception as e:
                    conn.rollback()
                    logger.error(f"ad_campaigns upsert failed for ad={insight.get('ad_id')} "
                                 f"date={insight.get('date_start')}: {e}")
                    errors += 1
                    continue
            conn.commit()
            logger.info(f"Upserted {upserted_ads} ad_campaigns rows")

            # Pre-build the variant resolver map so we don't do a SELECT per row.
            distinct_pids = {b["product_id"] for b in breakdown_rows if b.get("product_id")}
            resolver_map = build_variant_resolver_map(cur, distinct_pids)
            logger.info(
                f"Pre-resolved {len(resolver_map)} unique catalogue products "
                f"in 1 SELECT (was 1 per row before)"
            )

            # ad_campaign_products
            for breakdown in breakdown_rows:
                try:
                    row = format_ad_campaign_product_row(breakdown, resolver_map, now_iso)
                    if not row["ad_id"] or not row["date"] or not row["meta_product_id"]:
                        logger.warning(
                            f"Skipping malformed breakdown row: "
                            f"ad={row.get('ad_id')} date={row.get('date')} "
                            f"pid={row.get('meta_product_id')}"
                        )
                        continue
                    cur.execute(AD_CAMPAIGN_PRODUCTS_UPSERT, row)
                    upserted_products += 1
                except Exception as e:
                    conn.rollback()
                    logger.error(
                        f"ad_campaign_products upsert failed for "
                        f"ad={breakdown.get('ad_id')} "
                        f"pid={breakdown.get('product_id')}: {e}"
                    )
                    errors += 1
                    continue
            conn.commit()
            logger.info(f"Upserted {upserted_products} ad_campaign_products rows")

    finally:
        conn.close()

    logger.info(
        f"Sync complete — ad_campaigns: {upserted_ads} | "
        f"ad_campaign_products: {upserted_products} | errors: {errors}"
    )
    return upserted_ads, upserted_products, errors

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Meta Marketing API → Postgres sync")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK,
                        help=f"Days to look back (default {DEFAULT_LOOKBACK})")
    parser.add_argument("--single-ad", type=str, default=None,
                        help="Sync a single ad (debugging)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and summarise without writing to DB")
    args = parser.parse_args()

    logger.info(
        f"Meta sync starting — lookback={args.lookback_days}d "
        f"single_ad={args.single_ad or 'all'} "
        f"dry_run={args.dry_run}"
    )
    ads, products, errors = run_sync(
        lookback_days=args.lookback_days,
        single_ad_id=args.single_ad,
        dry_run=args.dry_run,
    )
    logger.info("Script complete")
    sys.exit(1 if errors > 0 else 0)
