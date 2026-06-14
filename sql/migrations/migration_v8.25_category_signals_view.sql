-- Migration v8.25 — v_category_signals
-- -------------------------------------
-- Stand-alone view returning one row per tracked category (Design, Ads,
-- Email) with the current health level + display signal text. Independent
-- of v_priority_tasks: the tiles at the top of tasks.html read from here
-- so they always render even when no Active task of that category exists.
--
-- The signal text matches v_priority_tasks.category_signal exactly so the
-- visual language across the page stays consistent. category_signal in
-- v_priority_tasks stays in place (other consumers may still use it);
-- this view just gives the UI a task-free path to the same info.
--
-- Levels: 'ok' green, 'watch' amber-blue (design only), 'warn' amber,
-- 'crit' red. sort_order pins display order: Design, Ads, Email.

CREATE OR REPLACE VIEW v_category_signals AS
WITH
design_pace AS (
  SELECT COUNT(DISTINCT product_handle) AS designs_30d
  FROM product_catalogue
  WHERE brand_id = 'your_brand_id'
    AND product_created_at >= NOW() - INTERVAL '30 days'
),
email_pace AS (
  SELECT COUNT(DISTINCT campaign_id) AS emails_7d
  FROM email_campaigns
  WHERE brand_id = 'your_brand_id'
    AND campaign_type = 'campaign'
    AND date >= CURRENT_DATE - INTERVAL '7 days'
),
email_scheduled AS (
  SELECT COUNT(*) AS scheduled_count,
         MIN(scheduled_at) AS next_scheduled
  FROM klaviyo_scheduled_campaigns
  WHERE brand_id = 'your_brand_id'
    AND status = 'scheduled'
    AND scheduled_at >= NOW()
    AND scheduled_at <= NOW() + INTERVAL '7 days'
),
ads_pace AS (
  SELECT COUNT(DISTINCT ad_id) AS new_ads_7d
  FROM (
    SELECT ad_id, MIN(date::date) AS first_seen
    FROM ad_campaigns
    WHERE brand_id = 'your_brand_id'
    GROUP BY ad_id
  ) x
  WHERE first_seen >= CURRENT_DATE - 7
)
SELECT
  'Design' AS category,
  1        AS sort_order,
  CASE
    WHEN designs_30d < 50 THEN 'crit'
    WHEN designs_30d < 75 THEN 'warn'
    WHEN designs_30d < 90 THEN 'watch'
    ELSE                       'ok'
  END AS level,
  CASE
    WHEN designs_30d < 50 THEN '🚨 Critical — ' || designs_30d || '/100 designs last 30 days'
    WHEN designs_30d < 75 THEN '⚠️ Behind — '   || designs_30d || '/100 designs last 30 days'
    WHEN designs_30d < 90 THEN '👀 Watch — '    || designs_30d || '/100 designs last 30 days'
    ELSE                       '✅ On track — '  || designs_30d || '/100 designs last 30 days'
  END AS signal
FROM design_pace

UNION ALL

SELECT
  'Ads' AS category,
  2     AS sort_order,
  CASE
    WHEN new_ads_7d = 0 THEN 'crit'
    WHEN new_ads_7d < 3 THEN 'warn'
    ELSE                     'ok'
  END,
  CASE
    WHEN new_ads_7d = 0 THEN '🚨 No new ads launched this week'
    WHEN new_ads_7d < 3 THEN '⚠️ Only ' || new_ads_7d || ' new ad(s) this week'
    ELSE                     '✅ Ads on track'
  END
FROM ads_pace

UNION ALL

SELECT
  'Email' AS category,
  3       AS sort_order,
  CASE
    WHEN es.scheduled_count > 0 THEN 'ok'
    WHEN ep.emails_7d = 0       THEN 'crit'
    WHEN ep.emails_7d < 2       THEN 'warn'
    ELSE                             'ok'
  END,
  CASE
    WHEN es.scheduled_count > 0 THEN
         '✅ ' || es.scheduled_count || ' campaign(s) scheduled — next: ' ||
         TO_CHAR(es.next_scheduled AT TIME ZONE 'Europe/London', 'Dy DD Mon HH24:MI')
    WHEN ep.emails_7d = 0 THEN '🚨 No campaigns sent or scheduled this week'
    WHEN ep.emails_7d < 2 THEN '⚠️ Only ' || ep.emails_7d || ' campaign(s) this week — none scheduled'
    ELSE                       '✅ Email on track'
  END
FROM email_pace ep, email_scheduled es

ORDER BY sort_order;
