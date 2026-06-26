"""
amex_category_rules.py
======================
CLI helper for managing amex_category_map. Mirrors monzo_category_rules.py
with one extra dimension — the `card` column lets a rule scope to
'platinum', 'nectar', or 'both'.

Manually-set categories (our_category_source='manual') are preserved
across --reapply, same as the Monzo flow.

Usage:
  amex_category_rules.py --list [--card platinum|nectar]
  amex_category_rules.py --unmatched [--card platinum|nectar] [--limit N]
  amex_category_rules.py --add --pattern P --field F --type T --category C --card K [--label L]
        F = merchant_name | description
        T = ilike | exact | starts_with
        K = platinum | nectar | both
  amex_category_rules.py --remove ID
  amex_category_rules.py --reapply [--dry-run]
  amex_category_rules.py --set-manual HASH --category C
  amex_category_rules.py --summary
"""

import argparse
import os
import sys
from collections import Counter
from decimal import Decimal

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

VALID_FIELDS = ("merchant_name", "description")
VALID_TYPES  = ("ilike", "exact", "starts_with")
VALID_CARDS  = ("platinum", "nectar", "both")


def db_connect():
    return psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
                            user=DB_USER, password=DB_PASSWORD)

# ─── List / unmatched ─────────────────────────────────────────────────────────

def cmd_list(conn, card_filter):
    sql = "SELECT id, pattern, match_field, match_type, our_category, category_label, card FROM amex_category_map"
    params = ()
    if card_filter:
        sql += " WHERE card = %s OR card = 'both'"
        params = (card_filter,)
    sql += " ORDER BY card, id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    print(f"{len(rows)} rule(s){' for card='+card_filter if card_filter else ''}:")
    print(f"  {'ID':>3}  {'card':<9}  {'pattern':<22}  {'field':<14}  {'type':<11}  {'category':<24}  label")
    print(f"  {'-'*3}  {'-'*9}  {'-'*22}  {'-'*14}  {'-'*11}  {'-'*24}  {'-'*30}")
    for r in rows:
        rid, pat, fld, mt, cat, lbl, card = r
        print(f"  {rid:>3}  {card:<9}  {pat[:22]:<22}  {fld:<14}  {mt:<11}  {cat:<24}  {lbl or ''}")


def cmd_unmatched(conn, card_filter, limit):
    where = ["needs_review = TRUE", "brand_id = %s"]
    params = [BRAND_ID]
    if card_filter:
        where.append("card = %s")
        params.append(card_filter)
    sql = f"""
        SELECT transaction_date, card, merchant_name, description,
               ROUND(amount_gbp::numeric, 2), source_file, transaction_hash
        FROM amex_transactions
        WHERE {' AND '.join(where)}
        ORDER BY transaction_date DESC LIMIT %s
    """
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        print("No needs_review rows. ✓")
        return
    print(f"{len(rows)} needs_review rows:")
    for d, c, m, desc, amt, sf, tx in rows:
        print(f"  {d}  {c:<8}  £{amt:>9}  {m or '-':<28}  {desc[:50]}")
        print(f"                                              tx={tx}  file={sf}")

    merch_ct = Counter(r[2] for r in rows if r[2])
    if merch_ct:
        print("\n  Top unmatched merchants:")
        for m, c in merch_ct.most_common(10):
            print(f"    {c:>3}x  {m}")

# ─── Add / remove ─────────────────────────────────────────────────────────────

def cmd_add(conn, pattern, field, mtype, category, card, label):
    if field not in VALID_FIELDS: sys.exit(f"--field must be one of {VALID_FIELDS}")
    if mtype not in VALID_TYPES:  sys.exit(f"--type must be one of {VALID_TYPES}")
    if card  not in VALID_CARDS:  sys.exit(f"--card must be one of {VALID_CARDS}")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO amex_category_map (pattern, match_field, match_type, our_category, category_label, card)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (pattern, match_field, card) DO UPDATE SET
                match_type     = EXCLUDED.match_type,
                our_category   = EXCLUDED.our_category,
                category_label = EXCLUDED.category_label
            RETURNING id, (xmax = 0) AS inserted
            """,
            (pattern, field, mtype, category, label, card),
        )
        rid, inserted = cur.fetchone()
    conn.commit()
    print(f"{'Added' if inserted else 'Updated'} rule #{rid}: {pattern!r} ({field} {mtype}, card={card}) -> {category}")


def cmd_remove(conn, rule_id):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM amex_category_map WHERE id = %s RETURNING pattern, our_category, card",
            (rule_id,),
        )
        row = cur.fetchone()
    if not row: sys.exit(f"No rule with id {rule_id}.")
    conn.commit()
    print(f"Removed rule #{rule_id}: {row[0]!r} ({row[2]}) -> {row[1]}")

# ─── Reapply ──────────────────────────────────────────────────────────────────

def categorise_one(merchant, desc, card, rules):
    for rule in rules:
        if rule["card"] not in (card, "both"):
            continue
        val = merchant if rule["match_field"] == "merchant_name" else desc
        if not val:
            continue
        pat = rule["pattern"]
        mt  = rule["match_type"]
        if   mt == "exact"       and val.lower() == pat.lower():          return rule["our_category"]
        elif mt == "ilike"       and pat.lower() in val.lower():          return rule["our_category"]
        elif mt == "starts_with" and val.lower().startswith(pat.lower()): return rule["our_category"]
    if "PAYMENT RECEIVED" in (desc or "").upper():
        return "AMEX_PAYMENT_RECEIVED"
    return None


def cmd_reapply(conn, dry_run):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM amex_category_map ORDER BY id")
        rules = list(cur.fetchall())
        cur.execute(
            """
            SELECT transaction_hash, merchant_name, description, card,
                   our_category, our_category_source
            FROM amex_transactions
            WHERE brand_id = %s AND our_category_source <> 'manual'
            """,
            (BRAND_ID,),
        )
        rows = cur.fetchall()

    changed = newly_cat = newly_unc = 0
    for r in rows:
        new_cat = categorise_one(r["merchant_name"], r["description"], r["card"], rules)
        if (new_cat or None) == (r["our_category"] or None):
            continue
        changed += 1
        if new_cat and not r["our_category"]:   newly_cat += 1
        if not new_cat and r["our_category"]:   newly_unc += 1

    print(f"Reapply over {len(rows)} auto-sourced row(s) using {len(rules)} rule(s):")
    print(f"  unchanged:           {len(rows) - changed}")
    print(f"  changed:             {changed}")
    print(f"    newly categorised: {newly_cat}")
    print(f"    became unmatched:  {newly_unc}")
    print(f"    re-bucketed:       {changed - newly_cat - newly_unc}")

    if dry_run or changed == 0:
        if dry_run: print("Dry run — no DB updates.")
        return

    with conn.cursor() as cur:
        for r in rows:
            new_cat = categorise_one(r["merchant_name"], r["description"], r["card"], rules)
            if (new_cat or None) == (r["our_category"] or None):
                continue
            cur.execute(
                "UPDATE amex_transactions SET our_category = %s, needs_review = %s "
                "WHERE transaction_hash = %s AND our_category_source <> 'manual'",
                (new_cat, new_cat is None, r["transaction_hash"]),
            )
    conn.commit()
    print(f"Wrote {changed} updates.")

# ─── Set manual / summary ─────────────────────────────────────────────────────

def cmd_set_manual(conn, tx_hash, category):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE amex_transactions SET our_category = %s, our_category_source = 'manual', "
            "needs_review = FALSE WHERE transaction_hash = %s AND brand_id = %s "
            "RETURNING description, ROUND(amount_gbp::numeric, 2)",
            (category, tx_hash, BRAND_ID),
        )
        row = cur.fetchone()
    if not row: sys.exit(f"No transaction with hash {tx_hash}.")
    conn.commit()
    desc, amt = row
    print(f"Set manual category {category!r} on {tx_hash} ({desc} £{amt}).")
    print("Future runs and --reapply will preserve this.")


def cmd_summary(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT card,
                   our_category,
                   COUNT(*),
                   ROUND(SUM(amount_gbp)::numeric, 2)
            FROM amex_transactions
            WHERE brand_id = %s
            GROUP BY card, our_category
            ORDER BY card, SUM(amount_gbp) DESC NULLS LAST
            """,
            (BRAND_ID,),
        )
        rows = cur.fetchall()
    print(f"{'card':<10}  {'category':<24}  {'txns':>5}  {'total_gbp':>12}")
    print(f"{'-'*10}  {'-'*24}  {'-'*5}  {'-'*12}")
    for card, cat, n, total in rows:
        print(f"{card:<10}  {(cat or 'UNCATEGORISED'):<24}  {n:>5}  {total or 0:>12}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Manage amex_category_map rules.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--list",       action="store_true")
    g.add_argument("--unmatched",  action="store_true")
    g.add_argument("--add",        action="store_true")
    g.add_argument("--remove",     type=int, metavar="ID")
    g.add_argument("--reapply",    action="store_true")
    g.add_argument("--set-manual", metavar="TX_HASH", dest="set_manual")
    g.add_argument("--summary",    action="store_true")

    p.add_argument("--pattern")
    p.add_argument("--field",    choices=VALID_FIELDS)
    p.add_argument("--type",     choices=VALID_TYPES, dest="match_type")
    p.add_argument("--category")
    p.add_argument("--card",     choices=VALID_CARDS)
    p.add_argument("--label")
    p.add_argument("--limit",    type=int, default=100)
    p.add_argument("--dry-run",  action="store_true")
    args = p.parse_args()

    conn = db_connect()
    try:
        if args.list:
            cmd_list(conn, args.card)
        elif args.unmatched:
            cmd_unmatched(conn, args.card, args.limit)
        elif args.add:
            if not all([args.pattern, args.field, args.match_type, args.category, args.card]):
                sys.exit("--add requires --pattern, --field, --type, --category and --card")
            cmd_add(conn, args.pattern, args.field, args.match_type, args.category, args.card, args.label)
        elif args.remove is not None:
            cmd_remove(conn, args.remove)
        elif args.reapply:
            cmd_reapply(conn, args.dry_run)
        elif args.set_manual:
            if not args.category:
                sys.exit("--set-manual requires --category")
            cmd_set_manual(conn, args.set_manual, args.category)
        elif args.summary:
            cmd_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
