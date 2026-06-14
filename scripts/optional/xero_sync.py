"""
xero_sync.py
============
Daily Xero bank account balance snapshot → xero_account_balances_daily.

Auth via OAuth2 pickle token at /opt/your_brand_id/credentials/xero_token.pickle
(generated locally on Windows — see /opt/your_brand_id/Xero_API_Context.md for
the one-time setup procedure).

Approach (one API call per tenant per run, plus the refresh + connections):
  1. POST /connect/token (refresh)      — always refresh, save back to pickle
  2. GET  /connections                  — discover tenant IDs
  3. GET  /Reports/BankSummary          — closing balances per bank account

The granted scopes (openid, offline_access, accounting.invoices.read,
accounting.banktransactions.read, accounting.reports.banksummary.read) do
NOT include accounting.settings.read, so we cannot call /Accounts to get
account metadata (type/code/currency). Those columns stay NULL in the DB
until the scopes are widened.

Critically: refresh tokens ROTATE on every refresh — we save the new token
back to disk immediately or the next run fails.

Usage:
    python3 xero_sync.py
    python3 xero_sync.py --dry-run
"""

import os
import sys
import pickle
import logging
import argparse
from datetime import datetime, timezone
import requests
import psycopg2
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv("/opt/your_brand_id/.env")

CLIENT_ID     = os.getenv("XERO_CLIENT_ID")
CLIENT_SECRET = os.getenv("XERO_CLIENT_SECRET")
TOKEN_FILE    = os.getenv(
    "XERO_TOKEN_FILE",
    "/opt/your_brand_id/credentials/xero_token.pickle",
)

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

BRAND_ID         = os.getenv("BRAND_ID", "your_brand_id")
REQUEST_TIMEOUT  = int(os.getenv("XERO_TIMEOUT", "30"))
LOG_FILE         = os.getenv("XERO_LOG_FILE", "logs/xero_sync.log")

TOKEN_URL   = "https://identity.xero.com/connect/token"
CONN_URL    = "https://api.xero.com/connections"
API_BASE    = "https://api.xero.com/api.xro/2.0"

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)

logger = logging.getLogger("xero_sync")
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

# ─── TOKEN MANAGEMENT ─────────────────────────────────────────────────────────

def load_token():
    if not os.path.exists(TOKEN_FILE):
        logger.critical(
            f"Xero token pickle not found at {TOKEN_FILE}. "
            "Re-run xero_auth.py locally — see /opt/your_brand_id/Xero_API_Context.md."
        )
        sys.exit(1)
    with open(TOKEN_FILE, "rb") as f:
        return pickle.load(f)


def save_token(token):
    """Persist refreshed token. Xero refresh tokens rotate — MUST save or
    next run will fail with invalid_grant."""
    tmp = TOKEN_FILE + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(token, f)
    os.replace(tmp, TOKEN_FILE)
    os.chmod(TOKEN_FILE, 0o600)


def refresh_access_token(token):
    """Exchange refresh_token for fresh access + refresh tokens.
    Always called at the start of a run — simpler than tracking expiry."""
    if not CLIENT_ID or not CLIENT_SECRET:
        logger.critical("XERO_CLIENT_ID or XERO_CLIENT_SECRET missing from .env")
        sys.exit(1)
    logger.info("Refreshing Xero access token")
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": token["refresh_token"],
        },
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.critical(f"Refresh failed: {resp.status_code} {resp.text[:300]}")
        sys.exit(1)
    new_token = resp.json()
    save_token(new_token)
    return new_token

# ─── XERO API ─────────────────────────────────────────────────────────────────

def fetch_tenants(access_token):
    """Returns list of {tenantId, tenantName, tenantType}."""
    resp = requests.get(
        CONN_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept":        "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json() or []


def fetch_bank_summary(access_token, tenant_id, from_date, to_date):
    """Bank Summary report — nested Xero report JSON."""
    url = f"{API_BASE}/Reports/BankSummary"
    resp = requests.get(
        url,
        headers={
            "Authorization":  f"Bearer {access_token}",
            "Xero-tenant-id": tenant_id,
            "Accept":         "application/json",
        },
        params={
            "fromDate": from_date.isoformat(),
            "toDate":   to_date.isoformat(),
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.error(f"BankSummary {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    return resp.json()

# ─── PARSING BANK SUMMARY ─────────────────────────────────────────────────────

def parse_bank_summary(report_body):
    """Extract per-bank-account closing balances from Xero's BankSummary.
    Returns list: [{account_id, account_name, balance}, ...].

    Xero structure:
      Reports[0].Rows[*]
        RowType=Header           — column headers, ignore
        RowType=Section          — contains Rows:
          RowType=Row            — one bank account, Cells:
              [0] = account name (Attributes contains account ID)
              [1..-2] = opening/cash in/cash out/movement
              [-1] = closing balance
          RowType=SummaryRow     — 'Total' across all accounts, ignore
    """
    out = []
    reports = report_body.get("Reports") or []
    if not reports:
        return out
    for section in reports[0].get("Rows", []) or []:
        if section.get("RowType") != "Section":
            continue
        for row in section.get("Rows", []) or []:
            if row.get("RowType") != "Row":
                continue
            cells = row.get("Cells") or []
            if not cells:
                continue
            name_cell = cells[0]
            name = name_cell.get("Value")
            account_id = None
            for attr in name_cell.get("Attributes", []) or []:
                if attr.get("Id") in ("account", "accountID", "AccountID"):
                    account_id = attr.get("Value")
                    break
            balance = None
            try:
                balance = float((cells[-1].get("Value") or "0").replace(",", ""))
            except (ValueError, TypeError):
                pass
            out.append({
                "account_id":   account_id,
                "account_name": name,
                "balance":      balance,
            })
    return out

# ─── UPSERT ───────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO xero_account_balances_daily (
    date, brand_id,
    xero_tenant_id, xero_account_id,
    account_name, account_code, account_type, bank_account_type, currency_code,
    balance, ytd_balance,
    fetched_at, synced_at
) VALUES (
    %(date)s, %(brand_id)s,
    %(xero_tenant_id)s, %(xero_account_id)s,
    %(account_name)s, %(account_code)s, %(account_type)s, %(bank_account_type)s, %(currency_code)s,
    %(balance)s, %(ytd_balance)s,
    %(fetched_at)s, %(synced_at)s
)
ON CONFLICT (date, brand_id, xero_tenant_id, xero_account_id) DO UPDATE SET
    account_name      = EXCLUDED.account_name,
    balance           = EXCLUDED.balance,
    fetched_at        = EXCLUDED.fetched_at,
    synced_at         = EXCLUDED.synced_at
"""

# ─── ORCHESTRATION ────────────────────────────────────────────────────────────

def build_row(today, tenant_id, entry, now_iso):
    """One DB row from a parsed BankSummary entry."""
    return {
        "date":              today,
        "brand_id":          BRAND_ID,
        "xero_tenant_id":    tenant_id,
        "xero_account_id":   entry["account_id"] or entry["account_name"] or "(unknown)",
        "account_name":      entry["account_name"],
        # These four require accounting.settings.read scope which isn't granted:
        "account_code":      None,
        "account_type":      None,
        "bank_account_type": None,
        "currency_code":     None,
        "balance":           entry["balance"],
        "ytd_balance":       None,
        "fetched_at":        now_iso,
        "synced_at":         now_iso,
    }


def run_sync(dry_run=False):
    test_db_connection()

    token = load_token()
    token = refresh_access_token(token)  # always refresh; saves back to pickle
    access_token = token["access_token"]

    tenants = fetch_tenants(access_token)
    if not tenants:
        logger.critical("No tenants returned from /connections — has the app been granted access?")
        sys.exit(1)

    today = datetime.now(timezone.utc).date()
    now_iso = datetime.now(timezone.utc).isoformat()
    all_rows = []

    for tenant in tenants:
        tid = tenant["tenantId"]
        tname = tenant.get("tenantName", "(unnamed)")
        logger.info(f"Syncing tenant: {tname} ({tid})")

        bank_report = fetch_bank_summary(access_token, tid, today, today)
        entries = parse_bank_summary(bank_report)
        logger.info(f"  parsed {len(entries)} bank account balance(s)")

        for entry in entries:
            row = build_row(today, tid, entry, now_iso)
            all_rows.append(row)
            logger.info(
                f"    {row['account_name']}: balance={row['balance']}"
            )

    if dry_run:
        logger.info(f"[DRY RUN] Would upsert {len(all_rows)} rows. No DB writes.")
        return 0, 0

    conn = get_db_connection()
    upserted = errors = 0
    try:
        with conn.cursor() as cur:
            for row in all_rows:
                try:
                    cur.execute(UPSERT_SQL, row)
                    upserted += 1
                except Exception as e:
                    conn.rollback()
                    logger.error(f"upsert failed for {row.get('account_name')}: {e}")
                    errors += 1
                    continue
        conn.commit()
    finally:
        conn.close()

    logger.info(f"Sync complete — upserted: {upserted} | errors: {errors}")
    return upserted, errors

# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Xero → Postgres balance sync")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + log without writing to DB")
    args = parser.parse_args()

    try:
        upserted, errors = run_sync(dry_run=args.dry_run)
        sys.exit(1 if errors > 0 else 0)
    except Exception as e:
        logger.error(f"xero_sync failed: {e}")
        sys.exit(1)
