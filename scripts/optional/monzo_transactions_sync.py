"""
monzo_transactions_sync.py
==========================
Ingest Monzo Business transactions into monzo_transactions and auto-categorise
against monzo_category_map. Refunds, fees, FX card payments and FPS transfers
all land in one flat table; categorisation rules are stored separately so we
can tune them without touching the script.

Token handling: the access token from monzo_auth.py expires in ~6h. This
script proactively refreshes whenever MONZO_TOKEN_EXPIRES is within 5 minutes
of now, and writes the rotated tokens straight back into /opt/your_brand_id/.env
(via the same line-preserving writer used by monzo_auth.py).

Backfill strategy: Monzo's /transactions endpoint rejects single windows
larger than ~365 days (HTTP 400 invalid_time_range). We chunk 90 days at a
time working BACKWARDS from today to the account-creation date 2024-11-25.
Within each window we paginate forwards using cursor-based since=<tx_id>.

Usage:
  python3 monzo_transactions_sync.py                  # incremental, last 7d
  python3 monzo_transactions_sync.py --dry-run        # incremental dry-run
  python3 monzo_transactions_sync.py --backfill       # full history
  python3 monzo_transactions_sync.py --backfill --dry-run
  python3 monzo_transactions_sync.py --review         # show needs_review rows
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

ENV_PATH = Path("/opt/your_brand_id/.env")
load_dotenv(ENV_PATH, override=True)

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
BRAND_ID    = os.getenv("BRAND_ID", "your_brand_id")

MONZO_CLIENT_ID     = os.environ["MONZO_CLIENT_ID"]
MONZO_CLIENT_SECRET = os.environ["MONZO_CLIENT_SECRET"]
MONZO_ACCOUNT_ID    = os.environ.get("MONZO_ACCOUNT_ID", "acc_0000AoPLn5jxPCWH2tjqXi")

MONZO_BASE      = "https://api.monzo.com"
LOG_FILE        = os.getenv("MONZO_LOG_FILE", "logs/monzo_transactions.log")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "60"))

# Backfill / pagination
BACKFILL_START_DATE = datetime(2024, 11, 25, tzinfo=timezone.utc)
WINDOW_DAYS         = 89   # Monzo SCA cap is strict-less-than 90 days; stay inside
PAGE_LIMIT          = 100
REFRESH_BUFFER_SECS = 300  # refresh if token expires within 5 minutes

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)
logger = logging.getLogger("monzo_transactions")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s [monzo_transactions] [%(levelname)s] %(message)s"))
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(fh)
logger.addHandler(sh)

# ─── ENV WRITER (matches monzo_auth.py) ───────────────────────────────────────

def _update_env_file(updates):
    """Replace or append the given keys in ENV_PATH; preserves every other line."""
    pattern = re.compile(r"^\s*(MONZO_(?:ACCESS_TOKEN|REFRESH_TOKEN|TOKEN_EXPIRES))\s*=")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    seen, out = set(), []
    for line in lines:
        m = pattern.match(line)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    with ENV_PATH.open("w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")

# ─── TOKEN HANDLING ───────────────────────────────────────────────────────────

def _refresh_token():
    refresh = os.environ.get("MONZO_REFRESH_TOKEN")
    if not refresh:
        sys.exit("ERROR: MONZO_REFRESH_TOKEN missing. Re-run monzo_auth.py.")
    logger.info("Refreshing Monzo access token (proactive)...")
    r = requests.post(
        f"{MONZO_BASE}/oauth2/token",
        data={
            "grant_type":    "refresh_token",
            "client_id":     MONZO_CLIENT_ID,
            "client_secret": MONZO_CLIENT_SECRET,
            "refresh_token": refresh,
        },
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"ERROR: token refresh failed ({r.status_code}): {r.text}")
    body         = r.json()
    new_access   = body["access_token"]
    new_refresh  = body.get("refresh_token") or refresh
    new_expires  = int(time.time()) + int(body.get("expires_in", 0))
    _update_env_file({
        "MONZO_ACCESS_TOKEN":  new_access,
        "MONZO_REFRESH_TOKEN": new_refresh,
        "MONZO_TOKEN_EXPIRES": str(new_expires),
    })
    os.environ["MONZO_ACCESS_TOKEN"]  = new_access
    os.environ["MONZO_REFRESH_TOKEN"] = new_refresh
    os.environ["MONZO_TOKEN_EXPIRES"] = str(new_expires)
    expires_dt = datetime.fromtimestamp(new_expires, tz=timezone.utc).isoformat()
    logger.info(f"  refreshed; new expiry {expires_dt}")
    return new_access


def get_access_token():
    """Return a usable access token, refreshing proactively if near expiry."""
    expires_at = int(os.environ.get("MONZO_TOKEN_EXPIRES", "0"))
    if expires_at - int(time.time()) < REFRESH_BUFFER_SECS:
        return _refresh_token()
    return os.environ["MONZO_ACCESS_TOKEN"]

# ─── MONZO HTTP ───────────────────────────────────────────────────────────────

class VerificationRequired(Exception):
    """Monzo SCA 90-day cap reached — older transactions require re-auth."""


def monzo_get(path, params=None):
    """GET with 401-token-refresh and explicit handling of Monzo's SCA 90-day cap."""
    token = get_access_token()
    r = requests.get(
        f"{MONZO_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    if r.status_code == 401:
        token = _refresh_token()
        r = requests.get(
            f"{MONZO_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    if r.status_code == 429:
        time.sleep(float(r.headers.get("Retry-After", "2")))
        return monzo_get(path, params)
    if r.status_code == 403:
        try:
            code = r.json().get("code", "")
        except Exception:
            code = ""
        if code == "forbidden.verification_required":
            raise VerificationRequired(
                "Monzo SCA 90-day cap reached — re-run monzo_auth.py on the Windows "
                "machine and start --backfill within ~5 minutes of approving the "
                "Monzo app prompt to fetch older history."
            )
    r.raise_for_status()
    return r


def _iso_z(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch_window(since_dt, before_dt):
    """
    Fetch every transaction in [since_dt, before_dt). First page uses
    timestamp `since`; subsequent pages cursor on the most recent tx id.
    """
    out = []
    since_param = _iso_z(since_dt)
    while True:
        params = {
            "account_id": MONZO_ACCOUNT_ID,
            "expand[]":   "merchant",
            "since":      since_param,
            "before":     _iso_z(before_dt),
            "limit":      PAGE_LIMIT,
        }
        r = monzo_get("/transactions", params)
        txns = r.json().get("transactions", []) or []
        if not txns:
            break
        out.extend(txns)
        if len(txns) < PAGE_LIMIT:
            break
        # Cursor pagination — newest id (response is ascending so last item is newest)
        since_param = txns[-1]["id"]
    return out


def iter_windows_backward(end_dt, start_dt, days=WINDOW_DAYS):
    cur = end_dt
    while cur > start_dt:
        prev = max(cur - timedelta(days=days), start_dt)
        yield prev, cur
        cur = prev

# ─── CATEGORISATION ───────────────────────────────────────────────────────────

def load_category_rules(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT pattern, match_field, match_type, our_category, category_label "
            "FROM monzo_category_map ORDER BY id"
        )
        return list(cur.fetchall())


def _field_value(tx, field):
    if field == "description":
        return (tx.get("description") or "").strip()
    if field == "counterparty_name":
        return ((tx.get("counterparty") or {}).get("name") or "").strip()
    if field == "merchant_name":
        return ((tx.get("merchant") or {}).get("name") or "").strip()
    return ""


def categorise(tx, rules):
    for rule in rules:
        val = _field_value(tx, rule["match_field"])
        if not val:
            continue
        pat = rule["pattern"]
        mt  = rule["match_type"]
        if   mt == "exact"       and val == pat:                            return rule["our_category"]
        elif mt == "ilike"       and pat.lower() in val.lower():            return rule["our_category"]
        elif mt == "starts_with" and val.lower().startswith(pat.lower()):   return rule["our_category"]
    return None

# ─── FORMAT / UPSERT ──────────────────────────────────────────────────────────

def _pence_to_pounds(v):
    if v is None:
        return None
    return (Decimal(int(v)) / Decimal(100)).quantize(Decimal("0.01"))


def format_row(tx, our_category):
    merchant     = tx.get("merchant") or {}
    counterparty = tx.get("counterparty") or {}
    return {
        "transaction_id":       tx["id"],
        "brand_id":             BRAND_ID,
        "account_id":           tx.get("account_id") or MONZO_ACCOUNT_ID,
        "created_at":           tx.get("created"),
        "settled_at":           tx.get("settled") or None,
        "description":          tx.get("description"),
        "amount_gbp":           _pence_to_pounds(tx.get("amount")),
        "currency":             tx.get("currency"),
        "local_amount_gbp":     _pence_to_pounds(tx.get("local_amount")),
        "local_currency":       tx.get("local_currency"),
        "monzo_category":       tx.get("category"),
        "scheme":               tx.get("scheme"),
        "counterparty_name":    counterparty.get("name"),
        "counterparty_account": counterparty.get("account_number"),
        "counterparty_sort":    counterparty.get("sort_code"),
        "merchant_id":          merchant.get("id"),
        "merchant_name":        merchant.get("name"),
        "merchant_category":    merchant.get("category"),
        "notes":                tx.get("notes"),
        "is_load":              tx.get("is_load"),
        "include_in_spending":  tx.get("include_in_spending"),
        "our_category":         our_category,
        "our_category_source":  "auto",
        "needs_review":         our_category is None,
        "raw_payload":          json.dumps(tx, ensure_ascii=False),
    }


UPSERT = """
INSERT INTO monzo_transactions (
    transaction_id, brand_id, account_id, created_at, settled_at, description,
    amount_gbp, currency, local_amount_gbp, local_currency,
    monzo_category, scheme,
    counterparty_name, counterparty_account, counterparty_sort,
    merchant_id, merchant_name, merchant_category,
    notes, is_load, include_in_spending,
    our_category, our_category_source, needs_review,
    raw_payload, synced_at
) VALUES (
    %(transaction_id)s, %(brand_id)s, %(account_id)s, %(created_at)s, %(settled_at)s, %(description)s,
    %(amount_gbp)s, %(currency)s, %(local_amount_gbp)s, %(local_currency)s,
    %(monzo_category)s, %(scheme)s,
    %(counterparty_name)s, %(counterparty_account)s, %(counterparty_sort)s,
    %(merchant_id)s, %(merchant_name)s, %(merchant_category)s,
    %(notes)s, %(is_load)s, %(include_in_spending)s,
    %(our_category)s, %(our_category_source)s, %(needs_review)s,
    %(raw_payload)s::jsonb, NOW()
) ON CONFLICT (transaction_id) DO UPDATE SET
    settled_at           = EXCLUDED.settled_at,
    description          = EXCLUDED.description,
    amount_gbp           = EXCLUDED.amount_gbp,
    currency             = EXCLUDED.currency,
    local_amount_gbp     = EXCLUDED.local_amount_gbp,
    local_currency       = EXCLUDED.local_currency,
    monzo_category       = EXCLUDED.monzo_category,
    scheme               = EXCLUDED.scheme,
    counterparty_name    = EXCLUDED.counterparty_name,
    counterparty_account = EXCLUDED.counterparty_account,
    counterparty_sort    = EXCLUDED.counterparty_sort,
    merchant_id          = EXCLUDED.merchant_id,
    merchant_name        = EXCLUDED.merchant_name,
    merchant_category    = EXCLUDED.merchant_category,
    notes                = EXCLUDED.notes,
    is_load              = EXCLUDED.is_load,
    include_in_spending  = EXCLUDED.include_in_spending,
    -- Preserve manual category overrides: only auto-update auto-sourced rows
    our_category         = CASE WHEN monzo_transactions.our_category_source = 'manual'
                                THEN monzo_transactions.our_category
                                ELSE EXCLUDED.our_category END,
    needs_review         = CASE WHEN monzo_transactions.our_category_source = 'manual'
                                THEN FALSE
                                ELSE EXCLUDED.needs_review END,
    raw_payload          = EXCLUDED.raw_payload,
    synced_at            = NOW();
"""


def db_connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)


def upsert_rows(conn, rows):
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, UPSERT, rows, page_size=200)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def cmd_review(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT created_at::date AS date,
                   transaction_id, description, counterparty_name, merchant_name,
                   ROUND(amount_gbp::numeric, 2) AS amount, monzo_category
            FROM monzo_transactions
            WHERE needs_review = TRUE AND brand_id = %s
            ORDER BY created_at DESC LIMIT 100
            """,
            (BRAND_ID,),
        )
        rows = cur.fetchall()
    if not rows:
        logger.info("No transactions currently flagged for review. ✓")
        return
    logger.info(f"{len(rows)} transactions flagged for review (most recent first):")
    for r in rows:
        date, tid, desc, cp, mer, amt, mc = r
        logger.info(f"  {date}  {amt:>9}  {desc or '-':<40}  cp={cp or '-'}  merch={mer or '-'}  monzo={mc or '-'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true",
                        help="Fetch all history from 2024-11-25 to today (90-day chunks).")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Fetch + classify + report; no DB writes.")
    parser.add_argument("--review",   action="store_true",
                        help="Print the current needs_review queue and exit (no API call).")
    args = parser.parse_args()

    conn = db_connect()
    try:
        if args.review:
            cmd_review(conn)
            return

        rules = load_category_rules(conn)
        logger.info(f"Loaded {len(rules)} categorisation rule(s) from monzo_category_map.")

        end_dt = datetime.now(timezone.utc).replace(microsecond=0)
        if args.backfill:
            start_dt = BACKFILL_START_DATE
            mode     = "BACKFILL"
        else:
            start_dt = end_dt - timedelta(days=7)
            mode     = "INCREMENTAL"
        dry = " (DRY RUN)" if args.dry_run else ""
        logger.info(f"monzo_transactions_sync starting — {mode}{dry}")
        logger.info(f"  window: {start_dt.date()} → {end_dt.date()}")

        t0 = time.monotonic()
        all_txns = []
        windows = list(iter_windows_backward(end_dt, start_dt))
        sca_capped_at = None
        for i, (ws, we) in enumerate(windows, 1):
            try:
                chunk = fetch_window(ws, we)
            except VerificationRequired:
                logger.warning(f"  window {i}/{len(windows)}  {ws.date()} → {we.date()}   SCA-blocked — older data needs fresh auth")
                sca_capped_at = ws
                break
            all_txns.extend(chunk)
            logger.info(f"  window {i}/{len(windows)}  {ws.date()} → {we.date()}   txns: {len(chunk):>4}   running total: {len(all_txns)}")
        api_secs = time.monotonic() - t0
        logger.info(f"  API time: {api_secs:.1f}s, total transactions: {len(all_txns)}")
        if sca_capped_at is not None:
            logger.warning(f"  ⚠  Backfill stopped at SCA boundary; could not fetch data older than {sca_capped_at.date()}.")
            logger.warning(f"     To get full history, re-run monzo_auth.py on Windows and start --backfill within ~5 minutes.")

        # Dedup defensively — overlapping window edges can repeat one row
        seen = set()
        deduped = []
        for tx in all_txns:
            tid = tx.get("id")
            if tid and tid not in seen:
                seen.add(tid)
                deduped.append(tx)
        if len(deduped) != len(all_txns):
            logger.info(f"  deduped: {len(all_txns)} -> {len(deduped)} (boundary overlap)")
        all_txns = deduped

        # Categorise everything
        rows = []
        for tx in all_txns:
            cat = categorise(tx, rules)
            rows.append(format_row(tx, cat))

        # Summary
        cat_counter = Counter(r["our_category"] or "UNCATEGORISED" for r in rows)
        cat_totals  = {}
        for r in rows:
            key = r["our_category"] or "UNCATEGORISED"
            cat_totals.setdefault(key, Decimal("0"))
            cat_totals[key] += r["amount_gbp"] or Decimal("0")
        needs_review = [r for r in rows if r["needs_review"]]
        inbound_n  = sum(1 for r in rows if (r["amount_gbp"] or 0) > 0)
        outbound_n = sum(1 for r in rows if (r["amount_gbp"] or 0) < 0)
        inbound_g  = sum((r["amount_gbp"] or Decimal(0)) for r in rows if (r["amount_gbp"] or 0) > 0)
        outbound_g = sum((r["amount_gbp"] or Decimal(0)) for r in rows if (r["amount_gbp"] or 0) < 0)
        dates = [r["created_at"] for r in rows if r["created_at"]]
        oldest = min(dates) if dates else None
        newest = max(dates) if dates else None

        bar = "=" * 60
        logger.info("")
        logger.info(bar)
        logger.info(f"MONZO INGESTION SUMMARY — {mode}{dry}")
        logger.info(bar)
        logger.info(f"Total transactions: {len(rows)}")
        logger.info(f"Date range:         {oldest}  →  {newest}")
        logger.info(f"Inbound:            {inbound_n:>4}  total +£{inbound_g:>10,.2f}")
        logger.info(f"Outbound:           {outbound_n:>4}  total  £{outbound_g:>10,.2f}")
        logger.info(f"Net:                       £{(inbound_g + outbound_g):>10,.2f}")
        logger.info("")
        logger.info("By our_category:")
        for cat, n in cat_counter.most_common():
            tot = cat_totals.get(cat, Decimal("0"))
            logger.info(f"  {cat:<22}  {n:>4} txns   £{tot:>10,.2f}")
        logger.info("")
        logger.info(f"needs_review (no rule matched): {len(needs_review)} / {len(rows)}")

        # Top descriptions and counterparties in needs_review — drives next pattern additions
        if needs_review:
            desc_ct = Counter()
            cp_ct   = Counter()
            mer_ct  = Counter()
            for r in needs_review:
                if r["description"]:       desc_ct[r["description"]] += 1
                if r["counterparty_name"]: cp_ct[r["counterparty_name"]] += 1
                if r["merchant_name"]:     mer_ct[r["merchant_name"]] += 1

            def top(ctr, label, n=12):
                if not ctr:
                    return
                logger.info(f"  Top {label} in needs_review:")
                for item, c in ctr.most_common(n):
                    logger.info(f"    {c:>3}x  {item}")

            logger.info("")
            top(desc_ct, "descriptions")
            logger.info("")
            top(cp_ct,   "counterparty names")
            logger.info("")
            top(mer_ct,  "merchant names")

            logger.info("")
            logger.info("First 8 needs_review rows (chronological newest first):")
            sample = sorted(needs_review, key=lambda r: r["created_at"] or "", reverse=True)[:8]
            for r in sample:
                amt = r["amount_gbp"] or Decimal(0)
                logger.info(f"  {(r['created_at'] or '?')[:10]}  {amt:>9}  "
                            f"desc={r['description'] or '-'}  "
                            f"cp={r['counterparty_name'] or '-'}  "
                            f"merch={r['merchant_name'] or '-'}")
        logger.info(bar)

        if args.dry_run:
            logger.info("")
            logger.info("Dry run complete. No DB writes. Re-run without --dry-run to apply.")
            return

        logger.info("Upserting monzo_transactions...")
        upsert_rows(conn, rows)
        conn.commit()
        logger.info(f"Committed {len(rows)} rows.")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info("Done.")


if __name__ == "__main__":
    main()
