# Setup Guide — POD Data Pipeline

## How to use this guide

Share this document with Claude Chat (claude.ai). Claude will work through each section with you, help you obtain API credentials, build your `.env` file, and generate a tailored Claude Code deployment brief at the end.

You don't need to be a developer to follow this. If a step feels confusing, paste the confusing bit into Claude Chat and it will explain.

## ⚠️ Critical — Timezone Configuration

**Set this correctly before running anything else.**

All date truncation in this pipeline uses `AT TIME ZONE 'Europe/London'` by default. If your business operates in a different timezone you must update this consistently across every sync script, SQL view and dashboard query before your first data sync.

Getting this wrong causes orders placed after 11pm local time to appear on the wrong date — a subtle but significant data quality issue that is very difficult to spot and painful to fix retrospectively.

### Questions to answer before setup:
- What timezone are you in? (e.g. `America/New_York`, `Australia/Sydney`)
- Where is your VPS located? (server timezone ≠ business timezone)
- What timezone do you want your data reported in? (always use your business timezone)

### What needs updating:
- All SQL views in `sql/views/` — every `AT TIME ZONE` reference
- All Metabase queries — every date truncation
- All sync scripts — any date-based filtering
- FastAPI endpoints — any date calculations

Claude Chat can help you find and update every occurrence during Step 2 onboarding. A global search for `Europe/London` across the repo will show every location that needs changing.

## ⚠️ Multi-Currency Warning

**This pipeline was built for a single-currency GBP store.**

If your store operates in a different currency (USD, AUD, NZD etc.) the pipeline will still work BUT metrics that combine data from multiple sources (MER, P&L, ROAS) may be incorrect if those sources use different currencies. Specifically:

- Meta ad spend is reported in your **ad account currency**
- Shopify revenue is in your **store currency**
- If these differ, MER = revenue/spend will be mathematically wrong

**Known affected metrics:** MER, ROAS, After Meta%, Operating Profit, Net Cash, all P&L views, live snapshot Est. Net.

**Workaround (manual):** Set your Meta ad account currency to match your store currency in Meta Business Manager.

**Planned fix:** A full FX/multi-currency layer with daily exchange rates is planned for a future release. This will add an `fx_rates` table and convert all values to a single reporting currency on ingest.

For now, single-currency stores (store currency = ad account currency) are fully supported. Multi-currency stores should proceed with caution and validate their MER figures against Shopify's native reports.

## Installing dependencies

```bash
# Core (required)
pip install -r requirements-core.txt

# Optional — install only what you need
pip install -r requirements-optional.txt
```

## Database setup

After `docker compose up -d`, apply migrations in version order (do NOT rely on Docker's auto-init — it sorts files alphabetically, which breaks v8.1 before v8.00 etc):

```bash
bash scripts/core/apply_migrations.sh
```

## Starting the dashboard API

```bash
# Copy and configure the systemd unit
sudo cp nginx/dashboard.service /etc/systemd/system/pod-dashboard.service
# Edit the unit file — replace YOUR_BRAND_ID and YOUR_SYSTEM_USER
sudo nano /etc/systemd/system/pod-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable pod-dashboard
sudo systemctl start pod-dashboard
sudo systemctl status pod-dashboard
```

### Nginx configuration

Update `nginx/dashboard.conf` — replace `YOUR_BRAND_ID` with your actual brand directory name before copying to `/etc/nginx/sites-available/`.

## Step 1 — Server

- What VPS provider are you using? (Hetzner recommended — CX32 at ~£8/mo)
- What is your server IP address?
- What OS? (Ubuntu 22.04 LTS recommended)

## Step 2 — Core services (required)

### Shopify

- Store URL (`yourstore.myshopify.com`)
- Do you have a Private App / Custom App set up with API access? If not, Claude will guide you.
  - Required scopes: `read_orders`, `read_products`, `read_customers`, `read_inventory`, `read_shopify_payments_payouts`

> **2026+ gotcha — permanent tokens.** Shopify no longer issues permanent access tokens for new Custom Apps. Use a Private App (if still available on your plan) or implement token rotation. Work through this with Claude Chat — Shopify's auth flow has changed significantly.

### Meta Ads

- Do you have a Meta Business Manager account?
- Do you have a Marketing API access token? If not, Claude will guide you through generating one via the Meta for Developers console.

> **Meta API gotcha — system-user approval.** During Meta API setup you may be asked to approve a system user via email. This email sometimes does not arrive. If stuck:
> 1. Try re-sending the approval from Meta Business Manager.
> 2. Check spam folders.
> 3. Use a personal Meta account's token temporarily for testing.
> 4. Work through this with Claude Chat — the Meta auth flow is the most common setup blocker.

### GA4

- Do you have GA4 installed on your store?
- Do you have a Google Cloud project with the GA4 Data API enabled? If not, Claude will guide you.

## Step 3 — Optional modules

Answer yes/no to each:

**Fulfilment:**
- [ ] Gelato
- [ ] Printify
- [ ] Other (note: manual COGS entry only — no automated reconciliation)

> **Printify — first run.** On first run, pass `--all-unmatched` to backfill historical unmatched orders:
> ```bash
> python3 scripts/optional/printify_postgres_sync.py --all-unmatched
> ```
> Subsequent nightly runs via cron do not need this flag.

**Email marketing:**
- [ ] Klaviyo
- [ ] Mailchimp (note: sync script not included — Claude Code can build one)
- [ ] Other
- [ ] None

**Payment providers (for fee reconciliation):**
- [ ] PayPal
- [ ] Klarna
- [ ] Stripe (note: sync script not included — Claude Code can build one)
- [ ] None / Shopify Payments only

**Bank transaction sync:**
- [ ] Monzo (UK — API available)
- [ ] Starling (UK — API available, script not included)
- [ ] Other bank with API
- [ ] CSV import only
- [ ] Skip bank sync

**Amex expenses:**
- [ ] Yes — I have Amex business cards (CSV import via `data/amex/inbox/`)
- [ ] No

**Additional analytics:**
- [ ] Google Search Console
- [ ] PageSpeed monitoring
- [ ] Xero (accounting sync, read-only)

## Step 4 — Email alerts

The nightly alert script sends a summary email via SMTP. Which provider will you use?

- [ ] Gmail (requires an app password — Gmail's regular password won't work)
- [ ] Fastmail
- [ ] SendGrid
- [ ] Mailgun
- [ ] Other SMTP provider

## Step 5 — What happens next

Once you've answered the above, Claude Chat will:
1. Guide you through obtaining each API credential.
2. Help you build your `.env` file (copy `.env.example` first).
3. Note which optional modules to include / exclude.
4. Generate a tailored Claude Code deployment brief.

The Claude Code brief will handle:
- Running the correct SQL migrations for your chosen modules
- Deploying only the sync scripts you need
- Setting up cron jobs
- Configuring Nginx and FastAPI
- First-run data sync and verification
- Nightly Postgres backup with retention

## Notes

- **Brand-specific constants** — a few scripts have configurable thresholds at the top (`COGS_RATE_DEFAULT`, best-seller cutoffs, late-delivery windows). Review before first run.
- **Backup script** — `scripts/core/backup_postgres.sh` ships pre-configured for a Docker-hosted Postgres. If you run Postgres natively (no container), replace the `docker exec` line with a direct `pg_dump` call.
- **PIN-gated forms** — the task manager has a server-side PIN check on Add Task and Log Time. Set `TASK_PIN` in `.env`. Fail-closed: if `TASK_PIN` is unset, every submission gets 401.
