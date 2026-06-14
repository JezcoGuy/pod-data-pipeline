# Cron schedule

All entries assume the pipeline is installed at `/opt/your_brand_id/`. Every line follows the same shape:

```
HH MM * * * cd /opt/your_brand_id && python3 scripts/<path>/<script>.py \
    >> logs/cron.log 2>&1 \
    || python3 scripts/core/send_alert.py "X FAILED" "Check logs/cron.log"
```

## Core (everyone)

```
05 *  * * *   live_sync.py                       # hourly: today's MER snapshot
00 01 * * *   core/backup_postgres.sh            # nightly Postgres dump
00 02 * * *   core/shopify_postgres_sync.py      # orders + line items
15 02 * * *   core/shopify_payouts_sync.py       # Shopify Payments payouts + lines
30 02 * * *   core/shopify_products_sync.py      # active products + variants
45 02 * * *   core/shopify_order_transactions_sync.py
00 03 * * *   core/customers_sync.py
15 03 * * *   core/meta_sync.py                  # ad campaigns + creatives
30 03 * * *   core/ga4_sync.py                   # sessions / products / pages
00 04 * * *   core/nightly_alert.py              # summary email
30 04 * * *   optional/pagespeed_sync.py         # if PageSpeed enabled
```

## Optional modules

```
00 02 * * *   optional/gelato_postgres_sync.py       # Gelato users
15 02 * * *   optional/printify_postgres_sync.py     # Printify users
45 03 * * *   optional/klaviyo_sync.py               # broadcast history
50 03 * * *   optional/klaviyo_sync.py --scheduled-only
00 04 * * *   optional/paypal_transactions_sync.py   # T0006 sales + T0200 FX
15 04 * * *   optional/klarna_settlements_sync.py    # SALE+FEE_PCT+FEE_FIXED rows
30 04 * * *   optional/monzo_transactions_sync.py    # UK bank
45 04 * * *   optional/gsc_sync.py                   # Search Console (if enabled)
00 05 * * *   optional/xero_sync.py                  # accounting sync (if enabled)
```

## Twice weekly

```
00 13 * * 2,5 optional/best_seller_update/best_seller_sync.py --execute --auto
```

## Manual / on-demand

- `optional/amex_sync.py` — run after dropping a new CSV into `data/amex/inbox/`
- `optional/ga4_sessions_backfill.py` — one-shot backfill for historical GA4 data
- `optional/import_manual_cogs.py` — manual COGS entry for non-API fulfilment providers
- `optional/monzo_auth.py` — re-auth Monzo every 89 days (PSD2 SCA window)

## Notes

- **Monzo PSD2 quirk** — Monzo's `/transactions` API only returns the last 89 days strictly, and only within ~5 minutes of a fresh auth. Schedule the re-auth + full sync as a manual pair every 89 days.
- **Backup before sync** — `backup_postgres.sh` is at 01:00 (one hour before any sync runs at 02:00). If a sync corrupts data, you can restore from the same day's backup.
- **Cron must `cd` first** — every line starts with `cd /opt/your_brand_id && ` so relative paths inside scripts (logs, .env) resolve consistently.
