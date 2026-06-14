# POD Data Pipeline

A self-hosted, open-source data warehouse for print-on-demand businesses. Consolidates 11+ data sources into a single PostgreSQL database with real-time dashboards, P&L reporting, product performance analytics, and an intelligent task manager.

## Running costs

~$10 USD/month for a VPS. Everything else is open source.

## What it does

- Nightly ingestion of Shopify orders, products, payouts, transactions and customers
- Meta Ads spend, performance and creative metadata
- GA4 sessions, pages and product analytics
- Fulfilment cost reconciliation from Gelato and/or Printify
- Payment-fee attribution from PayPal, Klarna and Shopify Payments
- UK bank-transaction ingest from Monzo + Amex CSV import
- Klaviyo campaign + scheduled-campaign tracking
- Search Console and PageSpeed Insights (optional)
- A P&L view that reconciles revenue → fees → COGS → ad spend → net
- A live snapshot endpoint refreshed every 5 minutes (today's MER)
- A priority task manager with category-signal banners driven by live data
- Nightly alert email summarising anomalies, late fulfilments, unmatched orders

## Architecture

```
                   ┌──────────────────┐
                   │  PostgreSQL 15   │  ← single source of truth
                   └──────────────────┘
                          ▲   ▲   ▲
              ┌───────────┘   │   └────────────┐
              │               │                │
       Python sync         Metabase        FastAPI
       scripts (cron)      (dashboards)    (HTML dashboards
       Shopify, Meta,                       + task manager)
       GA4, Klaviyo, ...                        │
                                                ▼
                                          mobile-first HTML
```

- **PostgreSQL** — central data store. All views, all reporting, all reconciliation.
- **Metabase** — exploratory dashboards and ad-hoc reporting.
- **FastAPI** — read endpoints for the HTML dashboards plus the task-manager CRUD.
- **NocoDB** — database GUI for manual editing of mapping tables (category rules etc).
- **Python sync scripts** — nightly cron jobs that pull from each data source. Each script is idempotent (upsert on natural keys) so re-runs are safe.

## Core data sources (required)

- Shopify (orders, products, payouts, transactions, customers)
- Meta Ads (campaigns, ad sets, ads, creatives)
- GA4 (sessions, pages, products)

## Optional modules

- **Fulfilment** — Gelato or Printify
- **Email** — Klaviyo (sent + scheduled)
- **Payments** — PayPal, Klarna (Shopify Payments fees come from Shopify itself)
- **Banking** — Monzo (UK), Amex (CSV import)
- **SEO** — Google Search Console, PageSpeed Insights
- **Accounting** — Xero (read-only sync)
- **Merchandising** — best-seller tag automation (twice-weekly)

## Quick start

See [SETUP.md](SETUP.md) for the full onboarding guide.

**Recommended:** work through `SETUP.md` with Claude Chat *before* deploying. Claude will help you obtain credentials, build your `.env`, and generate a tailored deployment brief for Claude Code.

## Requirements

- Linux VPS (Ubuntu 22.04+, 4 GB RAM minimum — Hetzner CX32 recommended)
- Docker + Docker Compose
- Python 3.10+
- A Shopify store with API access (Private App or Custom App)
- A Meta Business account with Marketing API access
- A GA4 property with the Data API enabled

## Setup

See [SETUP.md](SETUP.md).

## Repository layout

```
.
├── README.md                 you are here
├── SETUP.md                  onboarding guide (share with Claude Chat)
├── .env.example              copy → .env, fill in your credentials
├── docker-compose.yml        Postgres + Metabase + NocoDB
├── sql/
│   ├── migrations/           ordered DDL — apply v8.00 → v8.26 to bootstrap
│   └── views/                reporting views (re-runnable CREATE OR REPLACE)
├── scripts/
│   ├── core/                 required for any deployment
│   └── optional/             enable as needed
├── dashboard/                FastAPI app + mobile HTML
├── nginx/                    proxy config for the dashboard
└── docs/
    ├── architecture.md       data flow + table descriptions
    ├── cron_schedule.md      what runs when
    └── metabase_queries/     tested queries for the dashboards
```

## License

MIT. Use it, fork it, ship it. No warranty.
