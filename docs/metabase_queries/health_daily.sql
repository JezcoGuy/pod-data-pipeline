-- Health Daily
-- Card ID: 58
-- Collection: Root
-- Updated: 2026-06-06T11:31:52.096594Z
-- Extracted: 2026-06-14T10:36:25Z

WITH orders_data AS (
  SELECT
    (created_at AT TIME ZONE 'Europe/London') :: date AS day,
    COUNT(DISTINCT order_id) AS orders,
    SUM(line_items_count) AS items,
    ROUND(SUM(revenue_gbp) :: numeric, 2) AS revenue,
    ROUND(AVG(revenue_gbp) :: numeric, 2) AS aov,
    ROUND(
      SUM(
        CASE
          WHEN (created_at AT TIME ZONE 'Europe/London') :: date >= CURRENT_DATE - 1
          AND cogs_gbp = 0 THEN revenue_gbp * 0.421
          ELSE cogs_gbp
        END
      ) :: numeric,
      2
    ) AS cogs,
    ROUND(SUM(total_payment_fees) :: numeric, 2) AS fees,
    COUNT(
      DISTINCT CASE
        WHEN is_new_customer = false THEN order_id
      END
    ) AS returning_orders,
    ROUND(
      SUM(
        CASE
          WHEN shipping_country_code = 'GB' THEN revenue_gbp
          ELSE 0
        END
      ) :: numeric,
      2
    ) AS uk_rev,
    ROUND(
      SUM(
        CASE
          WHEN shipping_country_code = 'US' THEN revenue_gbp
          ELSE 0
        END
      ) :: numeric,
      2
    ) AS usa_rev,
    ROUND(
      SUM(
        CASE
          WHEN shipping_country_code IN (
            'AT',
            'BE',
            'BG',
            'HR',
            'CY',
            'CZ',
            'DK',
            'EE',
            'FI',
            'FR',
            'DE',
            'GR',
            'HU',
            'IE',
            'IT',
            'LV',
            'LT',
            'LU',
            'MT',
            'NL',
            'PL',
            'PT',
            'RO',
            'SK',
            'SI',
            'ES',
            'SE'
          ) THEN revenue_gbp
          ELSE 0
        END
      ) :: numeric,
      2
    ) AS eu_rev,
    ROUND(
      SUM(
        CASE
          WHEN shipping_country_code NOT IN (
            'GB',
            'US',
            'AT',
            'BE',
            'BG',
            'HR',
            'CY',
            'CZ',
            'DK',
            'EE',
            'FI',
            'FR',
            'DE',
            'GR',
            'HU',
            'IE',
            'IT',
            'LV',
            'LT',
            'LU',
            'MT',
            'NL',
            'PL',
            'PT',
            'RO',
            'SK',
            'SI',
            'ES',
            'SE'
          ) THEN revenue_gbp
          ELSE 0
        END
      ) :: numeric,
      2
    ) AS row_rev
  FROM
    orders
  WHERE
    brand_id = 'your_brand_id'
    AND financial_status NOT IN ('voided', 'refunded')
    AND created_at >= NOW() - INTERVAL '31 days'
  GROUP BY
    1
),
ads_data AS (
  SELECT
    (date AT TIME ZONE 'Europe/London') :: date AS day,
    ROUND(SUM(spend_gbp) :: numeric, 2) AS meta_spend,
    ROUND(SUM(spend_gbp) / NULLIF(SUM(impressions), 0) * 1000, 2) AS cpm,
    ROUND(SUM(spend_gbp) / NULLIF(SUM(clicks), 0), 2) AS cpc
  FROM
    ad_campaigns
  WHERE
    brand_id = 'your_brand_id'
    AND date >= CURRENT_DATE - INTERVAL '31 days'
  GROUP BY
    1
),
new_ads_data AS (
  SELECT
    first_seen AS day,
    COUNT(*) AS new_ads
  FROM
    (
      SELECT
        ad_id,
        MIN((date AT TIME ZONE 'Europe/London') :: date) AS first_seen
      FROM
        ad_campaigns
      WHERE
        brand_id = 'your_brand_id'
      GROUP BY
        ad_id
    ) x
  WHERE
    first_seen >= CURRENT_DATE - INTERVAL '31 days'
  GROUP BY
    first_seen
),
sessions_data AS (
  SELECT
    date AS day,
    sessions,
    atc_rate_pct,
    reached_checkout_pct,
    cr_pct
  FROM
    v_sessions_daily
  WHERE
    brand_id = 'your_brand_id'
    AND date >= CURRENT_DATE - INTERVAL '31 days'
),
popup_data AS (
  SELECT
    (date AT TIME ZONE 'Europe/London') :: date AS day,
    ROUND(SUM(submits) :: numeric / NULLIF(SUM(views), 0) * 100, 2) AS submit_pct
  FROM
    klaviyo_forms_daily
  WHERE
    brand_id = 'your_brand_id'
    AND date >= CURRENT_DATE - INTERVAL '31 days'
  GROUP BY
    1
),
designs_data AS (
  SELECT
    (product_created_at AT TIME ZONE 'Europe/London') :: date AS day,
    COUNT(DISTINCT product_handle) AS designs_uploaded
  FROM
    product_catalogue
  WHERE
    brand_id = 'your_brand_id'
    AND product_created_at >= NOW() - INTERVAL '31 days'
  GROUP BY
    1
),
email_data AS (
  SELECT
    date :: date AS day,
    COUNT(
      DISTINCT CASE
        WHEN campaign_type = 'campaign' THEN campaign_id
      END
    ) AS emails_sent,
    ROUND(
      SUM(
        CASE
          WHEN campaign_type = 'flow' THEN revenue_attributed
          WHEN campaign_type = 'campaign' THEN revenue_attributed * 0.5
          ELSE 0
        END
      ) :: numeric,
      2
    ) AS email_revenue
  FROM
    email_campaigns
  WHERE
    brand_id = 'your_brand_id'
    AND date >= CURRENT_DATE - INTERVAL '31 days'
  GROUP BY
    1
),
daily AS (
  SELECT
    o.day,
    o.revenue,
    o.orders,
    o.items,
    o.aov,
    o.cogs,
    o.fees,
    o.returning_orders,
    o.uk_rev,
    o.usa_rev,
    o.eu_rev,
    o.row_rev,
    COALESCE(a.meta_spend, 0) AS meta_spend,
    COALESCE(a.cpm, 0) AS cpm,
    COALESCE(a.cpc, 0) AS cpc,
    COALESCE(s.sessions, 0) AS sessions,
    COALESCE(s.atc_rate_pct, 0) AS atc_rate_pct,
    COALESCE(s.reached_checkout_pct, 0) AS reached_checkout_pct,
    COALESCE(s.cr_pct, 0) AS cr_pct,
    COALESCE(p.submit_pct, 0) AS submit_pct,
    COALESCE(d.designs_uploaded, 0) AS designs_uploaded,
    COALESCE(e.emails_sent, 0) AS emails_sent,
    COALESCE(e.email_revenue, 0) AS email_revenue,
    COALESCE(n.new_ads, 0) AS new_ads
  FROM
    orders_data o
    LEFT JOIN ads_data a ON a.day = o.day
    LEFT JOIN new_ads_data n ON n.day = o.day
    LEFT JOIN sessions_data s ON s.day = o.day
    LEFT JOIN popup_data p ON p.day = o.day
    LEFT JOIN designs_data d ON d.day = o.day
    LEFT JOIN email_data e ON e.day = o.day
  WHERE
    o.day < CURRENT_DATE
    AND o.revenue > 0
)
SELECT
  TO_CHAR(day, 'Dy DD Mon YYYY') AS "Date",
  CONCAT('£', revenue) AS "Revenue",
  CONCAT(
    ROUND(
      (
        (revenue - cogs - meta_spend - fees) / NULLIF(revenue, 0) * 100
      ) :: numeric,
      1
    ),
    '%'
  ) AS "PnL%",
  ROUND(revenue / NULLIF(meta_spend, 0), 2) AS "MER",
  CONCAT('£', meta_spend) AS "Ad Spend",
  CONCAT(
    ROUND((meta_spend / NULLIF(revenue, 0) * 100) :: numeric, 1),
    '%'
  ) AS "Ad%",
  CONCAT('£', cpc) AS "CPC",
  CONCAT('£', cpm) AS "CPM",
  CONCAT('£', aov) AS "AOV",
  CONCAT(cr_pct, '%') AS "CR%",
  CONCAT('£', cogs) AS "COGS",
  CONCAT(
    ROUND((cogs / NULLIF(revenue, 0) * 100) :: numeric, 1),
    '%'
  ) AS "COGS%",
  sessions AS "Sessions",
  CONCAT(atc_rate_pct, '%') AS "ATC%",
  CONCAT(reached_checkout_pct, '%') AS "RC%",
  orders AS "Orders",
  items AS "Items",
  CONCAT(
    ROUND(
      (returning_orders :: numeric / NULLIF(orders, 0) * 100) :: numeric,
      1
    ),
    '%'
  ) AS "RTN%",
  CONCAT(submit_pct, '%') AS "Submit%",
  CONCAT(
    ROUND((uk_rev / NULLIF(revenue, 0) * 100) :: numeric, 1),
    '%'
  ) AS "UK%",
  CONCAT(
    ROUND((usa_rev / NULLIF(revenue, 0) * 100) :: numeric, 1),
    '%'
  ) AS "USA%",
  CONCAT(
    ROUND((eu_rev / NULLIF(revenue, 0) * 100) :: numeric, 1),
    '%'
  ) AS "EU%",
  CONCAT(
    ROUND((row_rev / NULLIF(revenue, 0) * 100) :: numeric, 1),
    '%'
  ) AS "ROW%",
  designs_uploaded AS "Designs",
  CONCAT('£', email_revenue) AS "Email Rev",
  CONCAT(
    ROUND((email_revenue / NULLIF(revenue, 0) * 100) :: numeric, 1),
    '%'
  ) AS "Email%",
  emails_sent AS "Emails Sent",
  new_ads AS "New Ads",
  '-' AS "Social Posts",
  '-' AS "LOOX CR%",
  '-' AS "Notes"
FROM
  daily
ORDER BY
  day DESC;
