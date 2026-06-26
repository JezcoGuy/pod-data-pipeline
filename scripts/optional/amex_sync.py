"""
amex_sync.py
============
Ingest Amex monthly statement CSVs into `amex_transactions` and categorise
them via `amex_category_map`. Two cards are supported with --card:
  - platinum (COGS_FULFILMENT via Gelato, ADS_META via FACEBK)
  - nectar   (OVERHEAD_* for subscriptions and overheads)

Amex CSV format is minimal (Date, Description, Amount) and has NO transaction
ID. Dedupe uses a synthetic hash of date|description|amount|occurrence_index
|card so identical same-day charges (e.g. multiple Gelato £200 top-ups in
one day) all survive while re-uploading the same file is a safe no-op.

AMEX INGESTION — MONTHLY MANUAL PROCESS
=======================================
1. Download statement CSVs from online.americanexpress.com
2. Copy platinum CSV to  /opt/your_brand_id/data/amex/platinum/inbox/
3. Copy nectar   CSV to  /opt/your_brand_id/data/amex/nectar/inbox/
4. Run:  python3 scripts/amex_sync.py --card platinum
5. Run:  python3 scripts/amex_sync.py --card nectar
6. Review:  python3 scripts/amex_category_rules.py --unmatched
7. Add any new rules then:  python3 scripts/amex_category_rules.py --reapply

Usage:
  python3 amex_sync.py --card platinum
  python3 amex_sync.py --card nectar --dry-run
  python3 amex_sync.py --card platinum --backfill
  python3 amex_sync.py --review                # show needs_review rows
"""

import argparse
import csv
import glob
import hashlib
import logging
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# ─── ENV ──────────────────────────────────────────────────────────────────────

load_dotenv(override=True)

DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
BRAND_ID    = os.getenv("BRAND_ID", "your_brand_id")

DATA_ROOT = Path(os.getenv("AMEX_DATA_ROOT", "/opt/your_brand_id/data/amex"))
LOG_FILE  = os.getenv("AMEX_LOG_FILE", "logs/amex_sync.log")

VALID_CARDS = ("platinum", "nectar")

# ─── LOGGING ──────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_FILE) if os.path.dirname(LOG_FILE) else ".", exist_ok=True)
logger = logging.getLogger("amex_sync")
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILE)
fh.setFormatter(logging.Formatter("%(asctime)s [amex_sync] [%(levelname)s] %(message)s"))
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(fh)
logger.addHandler(sh)


def db_connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)

# ─── PARSING ──────────────────────────────────────────────────────────────────

MERCHANT_SPLIT_RE = re.compile(r"\s{3,}")
WHITESPACE_RE     = re.compile(r"\s+")


def extract_merchant(description):
    """Take the chunk before the first run of 3+ spaces (the location suffix)."""
    parts = MERCHANT_SPLIT_RE.split(description.strip(), maxsplit=1)
    return parts[0].strip()


def clean_description(description):
    """Collapse any whitespace run into a single space."""
    return WHITESPACE_RE.sub(" ", description.strip())


def parse_amount(s):
    return Decimal(str(s).strip().replace(",", ""))


def parse_date(s):
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


def classify_type(amount, description):
    if amount < 0:
        return "payment"
    upper = description.upper()
    if "REFUND" in upper or "CREDIT" in upper:
        return "credit"
    return "charge"


def make_hash(date_str, description, amount_str, occurrence_index, card):
    raw = f"{date_str}|{description}|{amount_str}|{occurrence_index}|{card}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def parse_csv(filepath, card):
    """Yield formatted row dicts ready for upsert."""
    seen = defaultdict(int)
    with open(filepath, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            date_str = raw["Date"]
            desc     = raw["Description"]
            amt_str  = raw["Amount"]
            if not date_str or not desc or amt_str in (None, ""):
                continue
            key = f"{date_str}|{desc}|{amt_str}"
            occurrence = seen[key]
            seen[key] += 1

            amount   = parse_amount(amt_str)
            date_obj = parse_date(date_str)
            merchant = extract_merchant(desc)
            yield {
                "transaction_hash":  make_hash(date_str, desc, amt_str, occurrence, card),
                "brand_id":          BRAND_ID,
                "card":              card,
                "transaction_date":  date_obj,
                "description":       desc,
                "description_clean": clean_description(desc),
                "merchant_name":     merchant,
                "amount_gbp":        amount,
                "transaction_type":  classify_type(amount, desc),
                "source_file":       os.path.basename(filepath),
            }

# ─── CATEGORISATION ───────────────────────────────────────────────────────────

def load_rules_for_card(conn, card):
    """Rules that apply to this card OR to both. Order by id (insertion order)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT pattern, match_field, match_type, our_category, category_label, card
            FROM amex_category_map
            WHERE card = %s OR card = 'both'
            ORDER BY id
            """,
            (card,),
        )
        return list(cur.fetchall())


def categorise(row, rules):
    merchant = (row.get("merchant_name") or "").strip()
    desc     = (row.get("description") or "").strip()
    for rule in rules:
        val = merchant if rule["match_field"] == "merchant_name" else desc
        if not val:
            continue
        pat = rule["pattern"]
        mt  = rule["match_type"]
        if   mt == "exact"       and val.lower() == pat.lower():           return rule["our_category"]
        elif mt == "ilike"       and pat.lower() in val.lower():           return rule["our_category"]
        elif mt == "starts_with" and val.lower().startswith(pat.lower()):  return rule["our_category"]
    # Safety net for Amex payments — keeps PAYMENT RECEIVED rows out of the
    # review queue even if the seed rule was deleted.
    if "PAYMENT RECEIVED" in desc.upper():
        return "AMEX_PAYMENT_RECEIVED"
    return None

# ─── DB ───────────────────────────────────────────────────────────────────────

UPSERT = """
INSERT INTO amex_transactions (
    transaction_hash, brand_id, card, transaction_date,
    description, description_clean, merchant_name,
    amount_gbp, transaction_type,
    our_category, our_category_source, needs_review,
    source_file, ingested_at
) VALUES (
    %(transaction_hash)s, %(brand_id)s, %(card)s, %(transaction_date)s,
    %(description)s, %(description_clean)s, %(merchant_name)s,
    %(amount_gbp)s, %(transaction_type)s,
    %(our_category)s, 'auto', %(needs_review)s,
    %(source_file)s, NOW()
) ON CONFLICT (transaction_hash) DO NOTHING;
"""


def file_already_ingested(conn, filename, card):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM amex_ingestion_log "
            "WHERE filename = %s AND card = %s AND status = 'success' LIMIT 1",
            (filename, card),
        )
        return cur.fetchone() is not None


def log_pending(conn, filename, card, rows_found):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amex_ingestion_log (filename, card, rows_found, status, ingested_at)
            VALUES (%s, %s, %s, 'pending', NOW())
            ON CONFLICT (filename, card) DO UPDATE SET
                rows_found = EXCLUDED.rows_found,
                status     = 'pending',
                error_message = NULL,
                ingested_at = NOW()
            RETURNING id
            """,
            (filename, card, rows_found),
        )
        return cur.fetchone()[0]


def log_success(conn, log_id, ingested, skipped):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE amex_ingestion_log SET status='success', rows_ingested=%s, "
            "rows_skipped=%s, error_message=NULL, ingested_at=NOW() WHERE id=%s",
            (ingested, skipped, log_id),
        )


def log_failure(conn, log_id, message):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE amex_ingestion_log SET status='failed', error_message=%s, ingested_at=NOW() "
            "WHERE id=%s",
            (message[:2000], log_id),
        )

# ─── PROCESS ──────────────────────────────────────────────────────────────────

def inbox_dir(card):     return DATA_ROOT / card / "inbox"
def processed_dir(card): return DATA_ROOT / card / "processed"


def process_file(conn, filepath, card, rules, dry_run, label):
    filename = os.path.basename(filepath)
    logger.info("")
    logger.info(f"── {label} {card}/{filename} ──")

    if file_already_ingested(conn, filename, card):
        logger.info(f"  already-ingested log entry exists — skipping (delete log row to re-ingest)")
        return {"skipped_file": True}

    rows = list(parse_csv(filepath, card))
    logger.info(f"  rows parsed: {len(rows)}")

    # Categorise in-memory
    for r in rows:
        cat = categorise(r, rules)
        r["our_category"] = cat
        r["needs_review"] = cat is None

    # Existing-hash check so dry-run can show "would-skip" counts correctly
    existing = set()
    if rows:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT transaction_hash FROM amex_transactions WHERE transaction_hash = ANY(%s)",
                ([r["transaction_hash"] for r in rows],),
            )
            existing = {h for (h,) in cur.fetchall()}

    new_rows  = [r for r in rows if r["transaction_hash"] not in existing]
    new_count = len(new_rows)
    skip_count = len(rows) - new_count

    # Summary for this file
    type_ct  = Counter(r["transaction_type"]               for r in rows)
    cat_ct   = Counter(r["our_category"] or "UNCATEGORISED" for r in rows)
    cat_tot  = defaultdict(lambda: Decimal("0"))
    for r in rows:
        cat_tot[r["our_category"] or "UNCATEGORISED"] += r["amount_gbp"]
    charge_sum  = sum((r["amount_gbp"] for r in rows if r["amount_gbp"] > 0), Decimal("0"))
    payment_sum = sum((r["amount_gbp"] for r in rows if r["amount_gbp"] < 0), Decimal("0"))
    needs_review_n = sum(1 for r in rows if r["needs_review"])
    dates = [r["transaction_date"] for r in rows]

    logger.info(f"  date range:  {min(dates)} → {max(dates)}" if dates else "  date range:  (empty)")
    logger.info(f"  by type:     {dict(type_ct)}")
    logger.info(f"  charges:     £{charge_sum:>12,.2f}   payments:  £{payment_sum:>12,.2f}")
    logger.info(f"  hash check:  would ingest {new_count}, skip {skip_count} (already in DB)")
    logger.info(f"  needs_review: {needs_review_n}")
    logger.info("  by category:")
    for cat, n in cat_ct.most_common():
        logger.info(f"    {cat:<28}  {n:>3} txns   £{cat_tot[cat]:>11,.2f}")

    if needs_review_n:
        review_rows = [r for r in rows if r["needs_review"]]
        merch_ct = Counter(r["merchant_name"] for r in review_rows if r["merchant_name"])
        if merch_ct:
            logger.info("  top unmatched merchants:")
            for m, c in merch_ct.most_common(10):
                logger.info(f"    {c:>2}x  {m}")

    if dry_run:
        return {
            "rows_found": len(rows), "would_ingest": new_count,
            "would_skip": skip_count, "needs_review": needs_review_n,
        }

    # Real ingest — write rows + move file + update log
    log_id = log_pending(conn, filename, card, rows_found=len(rows))
    try:
        if new_rows:
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, UPSERT, new_rows, page_size=200)
        processed_path = processed_dir(card) / filename
        shutil.move(filepath, str(processed_path))
        log_success(conn, log_id, ingested=new_count, skipped=skip_count)
        conn.commit()
        logger.info(f"  ✅ ingested {new_count}, skipped {skip_count} — moved to processed/")
    except Exception as e:
        log_failure(conn, log_id, str(e))
        conn.commit()
        logger.error(f"  ❌ {filename}: FAILED — {e} (file left in inbox)")
        raise

    return {"rows_found": len(rows), "ingested": new_count, "skipped": skip_count}


def cmd_process_card(conn, card, dry_run, backfill):
    rules = load_rules_for_card(conn, card)
    label = "DRY RUN" if dry_run else ("BACKFILL" if backfill else "INGEST")
    logger.info(f"amex_sync — card={card}  mode={label}  rules={len(rules)}")

    files = sorted(glob.glob(str(inbox_dir(card) / "*.csv")))
    if not files:
        logger.info(f"  no CSVs in {inbox_dir(card)} — nothing to do")
        return

    for f in files:
        process_file(conn, f, card, rules, dry_run, label)


def cmd_review(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT transaction_date, card, merchant_name, description,
                   ROUND(amount_gbp::numeric, 2), source_file, transaction_hash
            FROM amex_transactions
            WHERE needs_review = TRUE AND brand_id = %s
            ORDER BY transaction_date DESC LIMIT 100
            """,
            (BRAND_ID,),
        )
        rows = cur.fetchall()
    if not rows:
        logger.info("No needs_review rows. ✓")
        return
    logger.info(f"{len(rows)} needs_review rows (most recent first):")
    for d, c, m, desc, amt, sf, tx in rows:
        logger.info(f"  {d}  {c:<8}  £{amt:>9}  {m or '-':<28}  {desc[:50]}  ({tx})")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--card",     choices=VALID_CARDS, help="Card to process (platinum / nectar).")
    p.add_argument("--dry-run",  action="store_true", help="Parse + summarise; no DB writes, no file moves.")
    p.add_argument("--backfill", action="store_true", help="Same flow as default run; relabels log output.")
    p.add_argument("--review",   action="store_true", help="List existing needs_review rows and exit.")
    args = p.parse_args()

    conn = db_connect()
    try:
        if args.review:
            cmd_review(conn)
            return
        if not args.card:
            sys.exit("--card platinum|nectar is required (or pass --review)")
        cmd_process_card(conn, args.card, args.dry_run, args.backfill)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
