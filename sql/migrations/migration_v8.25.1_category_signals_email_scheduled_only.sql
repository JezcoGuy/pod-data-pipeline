-- Migration v8.25.1 — email tile is forward-looking only
-- --------------------------------------------------------
-- The email tile at the top of tasks.html should answer one question:
-- "Do I need to schedule more campaigns?" — past sends are irrelevant.
-- Strips the email_pace dependency (sent-in-7d count) and reduces the
-- email signal to a binary: anything scheduled → ok, nothing scheduled → crit.
--
-- v_priority_tasks's email_urgency CTE keeps its existing mixed logic
-- (sent + scheduled) — it scores TASKS not bars, and won't surface
-- anyway while no Email-category task exists. If we want to align that
-- later, it's a separate change.

CREATE OR REPLACE VIEW v_category_signals AS
WITH
design_pace AS (
  SELECT COUNT(DISTINCT product_handle) AS designs_30d
  FROM product_catalogue
  WHERE brand_id = 'your_brand_id'
    AND product_created_at >= NOW() - INTERVAL '30 days'
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

-- Email — binary, forward-looking only.
SELECT
  'Email' AS category,
  3       AS sort_order,
  CASE
    WHEN es.scheduled_count > 0 THEN 'ok'
    ELSE                             'crit'
  END,
  CASE
    WHEN es.scheduled_count > 0 THEN
         '✅ ' || es.scheduled_count || ' campaign(s) scheduled — next: ' ||
         TO_CHAR(es.next_scheduled AT TIME ZONE 'Europe/London', 'Dy DD Mon HH24:MI')
    ELSE '🚨 No campaigns scheduled — time to queue one'
  END
FROM email_scheduled es

ORDER BY sort_order;
