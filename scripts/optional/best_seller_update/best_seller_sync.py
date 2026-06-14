"""
best_seller_sync.py (v2)
========================
Syncs the `best_seller` tag on Shopify products to match the qualifying
products from Postgres. Collection management has been intentionally
dropped — v2 only touches product tags.

The Meta-facing smart collection ("Best Sellers") auto-tracks the tag rule
and is never touched here. Storefront collection ordering is handled
manually in Shopify admin.

Phase 1 (always runs): query DB, fetch currently tagged products,
generate terminal report + CSV.
Phase 2 (`--execute`): apply tag adds/removes, append to product_movements.log,
record run to best_seller_sync_runs (read by nightly_alert).

MAINTENANCE: Check logs/product_movements.log and logs/cron.log weekly to
verify the script is running correctly and making expected changes.
Cron runs: Tuesday and Friday at 13:00.

Usage:
    python3 best_seller_sync.py                  # dry-run (default)
    python3 best_seller_sync.py --dry-run        # explicit dry-run
    python3 best_seller_sync.py --execute        # apply changes (prompts)
    python3 best_seller_sync.py --execute --auto # apply changes (no prompt, for cron)
    python3 best_seller_sync.py --execute --limit 5
"""

import argparse
import csv
import logging
import os
import sys
from datetime import datetime

from db_query import get_designs_for_product_ids, get_qualifying_products, record_sync_run
from shopify_api import (
    add_tag_to_product,
    get_env,
    get_products_with_tag,
    remove_tag_from_product,
    to_gid,
)

# ─── Config ───────────────────────────────────────────────────────────────────

BEST_SELLER_TAG       = "best_seller"
SOFT_CAP_WARNING      = 200
HARD_CAP_EXECUTE      = 250
MAX_REMOVALS_PER_RUN  = 30
BRAND_ID              = "your_brand_id"

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
LOG_DIR         = os.path.join(BASE_DIR, "logs")
REPORT_DIR      = os.path.join(BASE_DIR, "reports")
MOVEMENTS_LOG   = os.path.join(LOG_DIR, "product_movements.log")


# ─── Logging ──────────────────────────────────────────────────────────────────

def setup_logging():
    """Run-log file (timestamped) + clean stdout for human-readable terminal output."""
    os.makedirs(LOG_DIR, exist_ok=True)
    run_stamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    log_path = os.path.join(LOG_DIR, f"best_seller_sync_{run_stamp}.log")

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    return log_path, run_stamp


# ─── Movement log (append-only) ───────────────────────────────────────────────

def append_movement(action, product_id, design, units_30d, momentum):
    """One line per tag change, permanent record. Never overwritten."""
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {action:<6} | {product_id} | {design:<22} | {units_30d:>2} units/30d | {momentum}\n"
    with open(MOVEMENTS_LOG, "a", encoding="utf-8") as f:
        f.write(line)


# ─── Report ───────────────────────────────────────────────────────────────────

def _trend_arrow(rank_change):
    if rank_change.startswith("UP"):
        return "↑ " + rank_change[3:]
    if rank_change.startswith("DOWN"):
        return "↓ " + rank_change[5:]
    if rank_change == "NEW":
        return "🆕 NEW"
    return "→"


def generate_report(products, currently_tagged_ids, run_stamp, mode):
    log = logging.getLogger()

    qualifying_ids = {p["product_id"] for p in products}
    to_add    = qualifying_ids - currently_tagged_ids
    to_remove = currently_tagged_ids - qualifying_ids
    to_keep   = qualifying_ids & currently_tagged_ids

    display_dt = datetime.now().strftime("%d %b %Y %H:%M")
    bar = "=" * 60

    log.info(bar)
    log.info("YOUR BRAND BEST SELLER TAG SYNC")
    log.info(f"Run date: {display_dt}")
    log.info(f"Mode: {mode}")
    log.info(bar)
    log.info("")
    log.info(f"📊 QUALIFYING PRODUCTS: {len(products)}")

    if len(products) > SOFT_CAP_WARNING:
        log.warning(f"⚠️  WARNING: {len(products)} qualifying products exceeds soft cap of {SOFT_CAP_WARNING}")
    if len(products) > HARD_CAP_EXECUTE:
        log.warning(f"🚨 HARD CAP EXCEEDED: {len(products)} > {HARD_CAP_EXECUTE}. --execute will be blocked.")
    if len(to_remove) > MAX_REMOVALS_PER_RUN:
        log.warning(f"🚨 REMOVAL SAFETY: {len(to_remove)} removals > limit of {MAX_REMOVALS_PER_RUN}. --execute will be blocked.")

    log.info("")
    log.info(f"✅ TO ADD ({len(to_add)} products):")
    for p in products:
        if p["product_id"] in to_add:
            log.info(f"   + {p['design']:<40} {p['u30']:>3} units/30d  {p['momentum']}")

    log.info("")
    log.info(f"🔄 KEEPING ({len(to_keep)} products):")
    for p in products:
        if p["product_id"] in to_keep:
            log.info(f"   = {p['design']:<40} {p['u30']:>3} units/30d  {_trend_arrow(p['rank_change'])}")

    log.info("")
    log.info(f"❌ TO REMOVE ({len(to_remove)} products):")
    for pid in sorted(to_remove):
        log.info(f"   - Product ID: {pid}")

    log.info("")
    log.info(bar)

    # CSV
    os.makedirs(REPORT_DIR, exist_ok=True)
    csv_path = os.path.join(REPORT_DIR, f"best_seller_sync_{run_stamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "action", "product_id", "design",
            "units_30d", "units_last_month", "units_lifetime",
            "momentum", "rank_change",
        ])
        writer.writeheader()
        for p in products:
            action = "ADD" if p["product_id"] in to_add else "KEEP"
            writer.writerow({
                "action": action,
                "product_id": p["product_id"],
                "design": p["design"],
                "units_30d": p["u30"],
                "units_last_month": p["u30_prior"],
                "units_lifetime": p["u_life"],
                "momentum": p["momentum"],
                "rank_change": p["rank_change"],
            })
        for pid in sorted(to_remove):
            writer.writerow({
                "action": "REMOVE",
                "product_id": pid,
                "design": "—",
                "units_30d": 0,
                "units_last_month": "",
                "units_lifetime": "",
                "momentum": "Below threshold",
                "rank_change": "",
            })

    log.info(f"📄 Report saved: {csv_path}")
    log.info("   Review this file before running with --execute")
    log.info(bar)

    return to_add, to_remove, to_keep, csv_path


# ─── Execute ──────────────────────────────────────────────────────────────────

def execute_sync(products, to_add, to_remove, to_keep, endpoint, headers,
                 tagged_products, args):
    log = logging.getLogger()

    # Safety gates
    if len(products) == 0:
        log.error("🚨 Execution aborted: zero qualifying products from DB.")
        return False
    if len(products) > HARD_CAP_EXECUTE:
        log.error(f"🚨 Execution aborted: hard cap exceeded ({len(products)} > {HARD_CAP_EXECUTE}).")
        return False
    if len(to_remove) > MAX_REMOVALS_PER_RUN:
        log.error(f"🚨 Execution aborted: removal safety limit exceeded ({len(to_remove)} > {MAX_REMOVALS_PER_RUN}).")
        return False

    if not args.auto:
        log.info("")
        log.info("⚠️  About to make changes to Shopify.")
        try:
            confirm = input("Type 'yes' to proceed: ")
        except EOFError:
            log.error("No stdin available and --auto not set. Aborting.")
            return False
        if confirm.strip().lower() != "yes":
            log.info("Aborted by user.")
            return False

    products_by_id = {p["product_id"]: p for p in products}
    cached_tags    = {p["id"].split("/")[-1]: p["tags"] for p in tagged_products}

    add_list    = list(to_add)
    remove_list = list(to_remove)
    if args.limit:
        add_list    = add_list[: args.limit]
        remove_list = remove_list[: args.limit]
        log.info(f"--limit {args.limit} applied: {len(add_list)} adds, {len(remove_list)} removes")

    # Look up human-readable designs for the to-remove ids (they're not in
    # the qualifying-products result anymore, but they're in product_catalogue).
    remove_designs = get_designs_for_product_ids(remove_list)

    log.info("")
    added_products   = []  # [{product_id, design}]
    removed_products = []

    # Adds
    for pid in add_list:
        p = products_by_id.get(pid, {"design": "?", "u30": 0, "momentum": "?"})
        gid = to_gid(pid)
        try:
            applied = add_tag_to_product(endpoint, headers, gid, BEST_SELLER_TAG)
            if applied:
                log.info(f"✅ Added best_seller tag: {p['design']} ({pid})")
                append_movement("ADD", pid, p["design"], p["u30"], p["momentum"])
                added_products.append({"product_id": pid, "design": p["design"]})
            else:
                log.info(f"   (already tagged) {p['design']} ({pid})")
        except Exception as e:
            log.error(f"❌ ADD FAILED for {pid} ({p['design']}): {e}")

    # Removes
    for pid in remove_list:
        gid = to_gid(pid)
        cached = cached_tags.get(pid)
        design = remove_designs.get(pid, "—")
        try:
            applied = remove_tag_from_product(endpoint, headers, gid, BEST_SELLER_TAG,
                                              cached_tags=cached)
            if applied:
                log.info(f"❌ Removed best_seller tag: {design} ({pid})")
                append_movement("REMOVE", pid, design, 0, "Below threshold")
                removed_products.append({"product_id": pid, "design": design})
            else:
                log.info(f"   (tag absent) {pid}")
        except Exception as e:
            log.error(f"❌ REMOVE FAILED for {pid}: {e}")

    # Record the run for the nightly alert.
    # `total_currently_tagged` = post-sync state: previously tagged
    # minus the removes we just applied, plus the adds we just applied.
    post_sync_tagged = len(tagged_products) - len(removed_products) + len(added_products)
    try:
        row_id = record_sync_run(
            brand_id=BRAND_ID,
            total_qualifying=len(products),
            total_currently_tagged=post_sync_tagged,
            added_products=added_products,
            removed_products=removed_products,
            keeps_count=len(to_keep),
            mode="execute",
        )
        log.info(f"📝 Recorded sync run #{row_id} to best_seller_sync_runs")
    except Exception as e:
        log.error(f"⚠️  Failed to record sync run to DB: {e}")

    return True


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true",
                        help="Apply tag changes to Shopify. Default is dry-run.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate report only. Default behaviour when --execute is absent.")
    parser.add_argument("--auto", action="store_true",
                        help="Skip confirmation prompt (for cron). Requires --execute.")
    parser.add_argument("--limit", type=int,
                        help="Cap the number of tag adds and removes (testing).")
    args = parser.parse_args()

    log_path, run_stamp = setup_logging()
    log = logging.getLogger()

    mode = "EXECUTE" if args.execute else "DRY RUN"
    log.info(f"best_seller_sync v2 starting (mode={mode}, limit={args.limit}, auto={args.auto})")
    log.info(f"Log file: {log_path}")

    log.info("Querying Postgres for qualifying products...")
    products = get_qualifying_products()
    log.info(f"  qualifying products: {len(products)}")

    endpoint, headers = get_env()
    log.info(f"Fetching products currently carrying '{BEST_SELLER_TAG}' from Shopify...")
    tagged_products = get_products_with_tag(endpoint, headers, BEST_SELLER_TAG)
    currently_tagged_ids = {p["id"].split("/")[-1] for p in tagged_products}
    log.info(f"  currently tagged: {len(currently_tagged_ids)}")
    log.info("")

    to_add, to_remove, to_keep, csv_path = generate_report(
        products, currently_tagged_ids, run_stamp, mode
    )

    if not args.execute:
        log.info("")
        log.info("Dry run complete. Re-run with --execute to apply changes.")
        return

    execute_sync(products, to_add, to_remove, to_keep, endpoint, headers,
                 tagged_products, args)
    log.info("")
    log.info("✅ Sync complete.")


if __name__ == "__main__":
    main()
