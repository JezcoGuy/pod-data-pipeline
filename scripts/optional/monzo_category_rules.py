"""
monzo_category_rules.py
=======================
CLI helper for managing monzo_category_map without raw SQL.

The Monzo sync writes one row per transaction with `our_category` derived
from the rules in monzo_category_map at ingestion time. Rules added AFTER a
transaction has been ingested don't retroactively apply — unless you run
this script's --reapply pass, which re-runs the same categorisation logic
across every stored row and updates the auto-sourced rows.

Manually-set categories (our_category_source='manual') are preserved
across both daily sync upserts and --reapply, so hand-labelling a one-off
transaction will stick.

Usage:
  monzo_category_rules.py --list
  monzo_category_rules.py --unmatched [--limit N]
  monzo_category_rules.py --add --pattern P --field F --type T --category C [--label L]
        F = description | counterparty_name | merchant_name
        T = exact | ilike | starts_with
  monzo_category_rules.py --remove ID
  monzo_category_rules.py --reapply [--dry-run]
        Re-classify every auto-sourced row against the current rule set.
        --dry-run reports how many rows would change without writing.
  monzo_category_rules.py --set-manual TX_ID --category C
        Set an explicit category on one transaction and lock it from
        future auto-overwrite (our_category_source='manual').
"""

import argparse
import os
import sys
from collections import Counter

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(override=True)

BRAND_ID    = os.getenv("BRAND_ID", "your_brand_id")
DB_HOST     = os.getenv("DB_HOST", "localhost")
DB_PORT     = os.getenv("DB_PORT", "5432")
DB_NAME     = os.getenv("DB_NAME")
DB_USER     = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

VALID_FIELDS = ("description", "counterparty_name", "merchant_name")
VALID_TYPES  = ("exact", "ilike", "starts_with")


def db_connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)


# ─── List / unmatched ─────────────────────────────────────────────────────────

def cmd_list(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, pattern, match_field, match_type, our_category, category_label "
            "FROM monzo_category_map ORDER BY id"
        )
        rows = cur.fetchall()
    print(f"{len(rows)} rule(s) in monzo_category_map:")
    print(f"  {'ID':>3}  {'pattern':<22}  {'field':<18}  {'type':<11}  {'category':<22}  label")
    print(f"  {'-'*3}  {'-'*22}  {'-'*18}  {'-'*11}  {'-'*22}  {'-'*30}")
    for r in rows:
        rid, pat, fld, mt, cat, lbl = r
        print(f"  {rid:>3}  {pat[:22]:<22}  {fld:<18}  {mt:<11}  {cat:<22}  {lbl or ''}")


def cmd_unmatched(conn, limit):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT transaction_id, created_at::date, description,
                   counterparty_name, merchant_name,
                   ROUND(amount_gbp::numeric, 2)
            FROM monzo_transactions
            WHERE brand_id = %s AND needs_review = TRUE
            ORDER BY created_at DESC LIMIT %s
            """,
            (BRAND_ID, limit),
        )
        rows = cur.fetchall()
    if not rows:
        print("No needs_review rows. ✓")
        return
    print(f"{len(rows)} needs_review row(s) (most recent first):")
    for r in rows:
        tid, date, desc, cp, mer, amt = r
        print(f"  {date}  {amt:>9}  desc={desc or '-'}")
        print(f"                       cp={cp or '-'}    merch={mer or '-'}    tx={tid}")

    # Frequency summary by candidate match field
    desc_ct = Counter(r[2] for r in rows if r[2])
    cp_ct   = Counter(r[3] for r in rows if r[3])
    mer_ct  = Counter(r[4] for r in rows if r[4])

    def top(label, ctr, n=8):
        if not ctr:
            return
        print(f"\n  Top {label}:")
        for k, c in ctr.most_common(n):
            print(f"    {c:>3}x  {k}")
    top("descriptions",       desc_ct)
    top("counterparty names", cp_ct)
    top("merchant names",     mer_ct)


# ─── Add / remove ─────────────────────────────────────────────────────────────

def cmd_add(conn, pattern, field, mtype, category, label):
    if field not in VALID_FIELDS:
        sys.exit(f"--field must be one of {VALID_FIELDS}, got {field!r}")
    if mtype not in VALID_TYPES:
        sys.exit(f"--type must be one of {VALID_TYPES}, got {mtype!r}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO monzo_category_map (pattern, match_field, match_type, our_category, category_label)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (pattern, match_field) DO UPDATE SET
                match_type     = EXCLUDED.match_type,
                our_category   = EXCLUDED.our_category,
                category_label = EXCLUDED.category_label
            RETURNING id, (xmax = 0) AS inserted
            """,
            (pattern, field, mtype, category, label),
        )
        rid, inserted = cur.fetchone()
    conn.commit()
    print(f"{'Added' if inserted else 'Updated'} rule #{rid}: {pattern!r} ({field} {mtype}) -> {category}")


def cmd_remove(conn, rule_id):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM monzo_category_map WHERE id = %s RETURNING pattern, our_category",
            (rule_id,),
        )
        row = cur.fetchone()
    if not row:
        sys.exit(f"No rule with id {rule_id}.")
    conn.commit()
    print(f"Removed rule #{rule_id}: {row[0]!r} -> {row[1]}")


# ─── Reapply categorisation ───────────────────────────────────────────────────

def _field_value(tx, field):
    if field == "description":
        return (tx.get("description") or "").strip()
    if field == "counterparty_name":
        cp = (tx.get("counterparty") or {})
        return (cp.get("name") or "").strip()
    if field == "merchant_name":
        m = (tx.get("merchant") or {})
        return (m.get("name") or "").strip()
    return ""


def categorise(payload, rules):
    """Same logic as monzo_transactions_sync.categorise — kept in sync by hand."""
    for rule in rules:
        val = _field_value(payload, rule["match_field"])
        if not val:
            continue
        pat = rule["pattern"]
        mt  = rule["match_type"]
        if   mt == "exact"       and val == pat:                          return rule["our_category"]
        elif mt == "ilike"       and pat.lower() in val.lower():          return rule["our_category"]
        elif mt == "starts_with" and val.lower().startswith(pat.lower()): return rule["our_category"]
    return None


def cmd_reapply(conn, dry_run):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM monzo_category_map ORDER BY id")
        rules = list(cur.fetchall())
        cur.execute(
            """
            SELECT transaction_id, raw_payload, our_category, our_category_source, needs_review
            FROM monzo_transactions
            WHERE brand_id = %s AND our_category_source <> 'manual'
            """,
            (BRAND_ID,),
        )
        rows = cur.fetchall()

    changed     = 0
    newly_cat   = 0
    newly_unc   = 0
    unchanged   = 0
    for r in rows:
        new_cat   = categorise(r["raw_payload"], rules)
        old_cat   = r["our_category"]
        if (new_cat or None) == (old_cat or None):
            unchanged += 1
            continue
        changed += 1
        if new_cat and not old_cat:   newly_cat += 1
        if not new_cat and old_cat:   newly_unc += 1

    print(f"Reapply over {len(rows)} auto-sourced row(s) using {len(rules)} rule(s):")
    print(f"  unchanged:           {unchanged}")
    print(f"  changed:             {changed}")
    print(f"    newly categorised: {newly_cat}")
    print(f"    became unmatched:  {newly_unc}")
    print(f"    re-bucketed:       {changed - newly_cat - newly_unc}")

    if dry_run:
        print("Dry run — no DB updates.")
        return

    if changed == 0:
        print("No changes to apply.")
        return

    with conn.cursor() as cur:
        for r in rows:
            new_cat = categorise(r["raw_payload"], rules)
            old_cat = r["our_category"]
            if (new_cat or None) == (old_cat or None):
                continue
            cur.execute(
                "UPDATE monzo_transactions SET our_category = %s, needs_review = %s "
                "WHERE transaction_id = %s AND our_category_source <> 'manual'",
                (new_cat, new_cat is None, r["transaction_id"]),
            )
    conn.commit()
    print(f"Wrote {changed} updates.")


# ─── Manual override ──────────────────────────────────────────────────────────

def cmd_set_manual(conn, transaction_id, category):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE monzo_transactions
            SET our_category = %s, our_category_source = 'manual', needs_review = FALSE
            WHERE transaction_id = %s AND brand_id = %s
            RETURNING description, ROUND(amount_gbp::numeric, 2)
            """,
            (category, transaction_id, BRAND_ID),
        )
        row = cur.fetchone()
    if not row:
        sys.exit(f"No transaction with id {transaction_id}.")
    conn.commit()
    desc, amt = row
    print(f"Set manual category {category!r} on {transaction_id} ({desc} {amt}).")
    print("Future sync runs will preserve this — it won't be overwritten by auto-categorisation.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Manage monzo_category_map rules.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list",        action="store_true")
    g.add_argument("--unmatched",   action="store_true")
    g.add_argument("--add",         action="store_true")
    g.add_argument("--remove",      type=int, metavar="ID")
    g.add_argument("--reapply",     action="store_true")
    g.add_argument("--set-manual",  metavar="TX_ID", dest="set_manual")

    p.add_argument("--pattern")
    p.add_argument("--field",    choices=VALID_FIELDS)
    p.add_argument("--type",     choices=VALID_TYPES, dest="match_type")
    p.add_argument("--category")
    p.add_argument("--label")
    p.add_argument("--limit",    type=int, default=50)
    p.add_argument("--dry-run",  action="store_true")
    args = p.parse_args()

    conn = db_connect()
    try:
        if args.list:
            cmd_list(conn)
        elif args.unmatched:
            cmd_unmatched(conn, args.limit)
        elif args.add:
            if not all([args.pattern, args.field, args.match_type, args.category]):
                sys.exit("--add requires --pattern, --field, --type and --category")
            cmd_add(conn, args.pattern, args.field, args.match_type, args.category, args.label)
        elif args.remove is not None:
            cmd_remove(conn, args.remove)
        elif args.reapply:
            cmd_reapply(conn, args.dry_run)
        elif args.set_manual:
            if not args.category:
                sys.exit("--set-manual requires --category")
            cmd_set_manual(conn, args.set_manual, args.category)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
