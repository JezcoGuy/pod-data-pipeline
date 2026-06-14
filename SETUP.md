# Setup Guide — POD Data Pipeline

## How to use this guide

Share this document with Claude Chat (claude.ai). Claude will work through each section with you, help you obtain API credentials, build your `.env` file, and generate a tailored Claude Code deployment brief at the end.

You don't need to be a developer to follow this. If a step feels confusing, paste the confusing bit into Claude Chat and it will explain.

## Step 1 — Server

- What VPS provider are you using? (Hetzner recommended — CX32 at ~£8/mo)
- What is your server IP address?
- What OS? (Ubuntu 22.04 LTS recommended)

## Step 2 — Core services (required)

### Shopify

- Store URL (`yourstore.myshopify.com`)
- Do you have a Private App / Custom App set up with API access? If not, Claude will guide you.
  - Required scopes: `read_orders`, `read_products`, `read_customers`, `read_inventory`, `read_shopify_payments_payouts`

### Meta Ads

- Do you have a Meta Business Manager account?
- Do you have a Marketing API access token? If not, Claude will guide you through generating one via the Meta for Developers console.

### GA4

- Do you have GA4 installed on your store?
- Do you have a Google Cloud project with the GA4 Data API enabled? If not, Claude will guide you.

## Step 3 — Optional modules

Answer yes/no to each:

**Fulfilment:**
- [ ] Gelato
- [ ] Printify
- [ ] Other (note: manual COGS entry only — no automated reconciliation)

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
