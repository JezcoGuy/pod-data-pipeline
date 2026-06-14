# Architecture

## Data flow

```
External APIs           Sync scripts (cron)         PostgreSQL              Consumers
─────────────           ───────────────────         ──────────              ─────────
Shopify          ───▶   shopify_*_sync.py    ───▶   orders, products,      Metabase
Meta Marketing   ───▶   meta_sync.py         ───▶   ad_campaigns,          (dashboards)
GA4 Data         ───▶   ga4_sync.py          ───▶   ga4_sessions_daily,
Klaviyo          ───▶   klaviyo_sync.py      ───▶   email_campaigns,       FastAPI
Gelato/Printify  ───▶   *_postgres_sync.py   ───▶   order_fulfilment,      (HTML
PayPal/Klarna    ───▶   *_sync.py            ───▶   *_transactions,        dashboards,
Monzo            ───▶   monzo_*_sync.py      ───▶   monzo_transactions,    task mgr)
Amex (CSV)       ───▶   amex_sync.py         ───▶   amex_transactions,
PageSpeed/GSC    ───▶   *_sync.py            ───▶   pagespeed_*, gsc_*     NocoDB
                                                                            (manual
                        nightly_alert.py   ───────▶ SMTP                    editing)
                        backup_postgres.sh ───────▶ /backups/postgres/
```

Every sync script:
- Loads `.env` for credentials.
- Connects to Postgres via psycopg2.
- Pulls from its API (with retry + 429/timeout backoff).
- Upserts on a natural key (so re-runs are idempotent).
- Logs to `logs/cron.log`.
- On failure: cron's `||` fallback sends a `[Your Brand]` alert email via `send_alert.py`.

## Tables — what lives where

| Table | Source | Purpose |
|---|---|---|
| `orders` | shopify_postgres_sync | One row per Shopify order, with revenue, fees, COGS attribution |
| `order_line_items` | same | Variant-level line items |
| `product_catalogue` | shopify_products_sync | Active products + variants (handle, title, SKU, price) |
| `customers` | customers_sync | One row per Shopify customer |
| `shopify_payouts` | shopify_payouts_sync | Aggregated daily payouts |
| `shopify_payout_lines` | same | Per-order payout breakdown (true source of fee math) |
| `shopify_order_transactions` | shopify_order_transactions_sync | Individual auth/capture/refund events per order |
| `ad_campaigns` | meta_sync | Daily Meta ad spend at campaign/ad-set/ad level |
| `ad_creative_metadata` | same | Body, CTA, image URL etc. for ranking creatives |
| `ad_campaign_products` | same | Best-effort mapping of ads → products via UTMs |
| `ga4_sessions_daily` | ga4_sync | Daily channel attribution |
| `ga4_products_daily` | same | Daily product views, add-to-cart, purchases |
| `ga4_pages_daily` | same | Daily page-level traffic |
| `email_campaigns` | klaviyo_sync | One row per Klaviyo broadcast send |
| `klaviyo_scheduled_campaigns` | same `--scheduled-only` | Upcoming queued sends |
| `order_fulfilments` | gelato_postgres_sync / printify_postgres_sync | Per-order fulfilment events + true COGS |
| `paypal_transactions` | paypal_transactions_sync | T0006 sales + T0200 FX rows |
| `klarna_settlements` | klarna_settlements_sync | SALE + FEE_PCT + FEE_FIXED triplets per capture |
| `monzo_transactions` | monzo_transactions_sync | Categorised bank activity |
| `amex_transactions` | amex_sync | Categorised Amex card activity (CSV import) |
| `pagespeed_audits` | pagespeed_sync | LCP, FID, CLS scores per URL |
| `gsc_*` | gsc_sync | Search Console clicks / impressions |
| `tasks` + `time_log` | dashboard API | Self-managed task list and time tracking |

## Views — what's used where

| View | Used by | Description |
|---|---|---|
| `v_pl_monthly` | Metabase, /pl-monthly | One row per month with revenue → fees → COGS → ad spend → net |
| `v_priority_tasks` | /tasks/priority | Live-scored Active tasks with urgency-tier sort |
| `v_category_signals` | /category-signals | Always-on Design / Ads / Email status bars |
| `v_sessions_daily` | Metabase | Channel + device session breakdown |
| `v_variant_sales` | Metabase | Variant-level revenue + units (90-day rolling) |
| `v_product_sales` | Metabase | Product-level rollup |
| `v_ga4_funnel_daily` | Metabase | Daily conversion funnel |
| `v_new_design_testing` | Metabase | Performance of products launched in last 30 days |
| `v_catalogue_testing_report` | Metabase | Which products to cull |
| `v_shopify_payout_summary` | Metabase | Daily Shopify Payments reconciliation |
| `v_fee_by_payment_gateway` | Metabase | Per-gateway fee aggregation (PayPal, Klarna, Shopify Payments) |
| `v_paypal_summary`, `v_klarna_summary` | Metabase | Provider-specific reconciliation |
| `v_amex_monthly`, `v_amex_needs_review` | Metabase, manual review | Expense categorisation + uncategorised queue |
| `v_monzo_monthly`, `v_monzo_needs_review` | same | Bank categorisation + uncategorised queue |
| `order_fulfilment_status` | Metabase | Joined view of orders + fulfilment for late-delivery alerts |

## Sync script schedule

See [cron_schedule.md](cron_schedule.md).

## Alert system

Two channels:

1. **Per-script failure alerts.** Every cron line is `cmd || python3 scripts/core/send_alert.py "X FAILED" "Check logs/cron.log"`. So if any sync exits non-zero, you get an email within minutes.
2. **Nightly summary.** `nightly_alert.py` runs at 04:00 and queries the DB for: orders not yet fulfilled past `GELATO_ALERT_HOURS`, returned orders past `RETURNED_ALERT_DAYS`, late deliveries past `LATE_DELIVERY_DAYS`, refunds past `REFUND_ALERT_DAYS`, unmatched orders, and any uncategorised bank/expense rows. Emails a digest only if there's something to flag.

Individual sync scripts can also append rows to a "scratch" alert file mid-run via `alert_writer.write_alert_section()` — the nightly summary picks those up.
