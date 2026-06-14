# Metabase Queries

These SQL files were extracted from a live Metabase instance using the
Metabase API (`/api/dataset/native`). They represent the actual SQL
Metabase runs against PostgreSQL for each saved question.

## Important notes
- All brand-specific identifiers have been replaced with `YOUR_BRAND_ID`
- Queries reference views (`v_pl_monthly`, `v_priority_tasks` etc.) defined in `sql/views/`
- Some queries may need adapting for your specific data sources
- Not all queries will be relevant — use as reference and build your own with Claude Chat assistance during Step 2 onboarding
- Metabase sample-dataset queries (Orders, People, Products demo data) and AI-generated placeholder cards were excluded from this set

## Dashboard mapping

| File | Dashboard | Tab |
|---|---|---|
| `p_l_daily.sql` | P&L | Daily Last 30 |
| `current_month.sql` | P&L | Current Month |
| `p_l_trends.sql` | P&L | Per Month Summary |
| `p_l_reconciliation.sql` | P&L | Cash Reconciliation |
| `expenses_breakdown.sql` | P&L | Expenses |
| `cogs_regional_breakdown.sql` | P&L | COGS Analysis |
| `cogs_by_region.sql` | P&L | COGS Analysis (alt rollup) |
| `est_net_profit.sql` | P&L | Net Profit Estimate |
| `health_daily.sql` | Health | Daily Table |
| `health_averages.sql` | Health | Averages |
| `pagespeed_performance.sql` | Health | Pagespeed |
| `meta_spend.sql` | Health | Meta Spend |
| `mer.sql` | Health | MER |
| `product_performance.sql` | Product Performance | Product Trends |
| `best_sellers.sql` | Product Performance | Best Sellers |
| `culling_report.sql` | Product Performance | Culling |
| `regional_heroes.sql` | Product Performance | Regional Heroes |
| `colour_trends.sql` | Product Performance | Colour Trends |
| `new_design_testing_testable_5_spend.sql` | New Design Testing | Testable (≥ £5 spend) |
| `new_design_testing_hidden_signals_under_5_spend.sql` | New Design Testing | Hidden Signals (< £5 spend) |
| `new_design_testing_organic_performers_no_meta_spend.sql` | New Design Testing | Organic Performers |
| `new_design_testing_watch_investigate.sql` | New Design Testing | Watch / Investigate |
| `late_deliveries.sql` | Fulfilment | Late Deliveries |
| `unmatched_gelato_orders.sql` | Fulfilment | Unmatched Gelato |
| `aov.sql` | Snapshot | AOV |
| `orders.sql` | Snapshot | Orders today |
| `items.sql` | Snapshot | Items today |
| `revenue_2.sql` | Snapshot | Revenue today |
| `unique_customers_per_month.sql` | Customers | Unique per Month |
