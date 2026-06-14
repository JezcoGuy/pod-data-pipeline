-- Migration v8.24.1 — v_priority_tasks email signal includes scheduled campaigns
-- ------------------------------------------------------------------------------
-- Pairs with migration v8.24 (klaviyo_scheduled_campaigns table). With a
-- campaign scheduled in the next 7 days, the email category's urgency
-- bonus drops to 0 (no longer pushing email tasks up the priority list)
-- and the signal flips from a red "no campaigns sent" to a green
-- "campaign scheduled — next: <day, date, time>" note.
--
-- Filename uses v8.24.1 rather than v8.24 (which the brief specified)
-- because the prior v8.24 file already exists for the table create.

CREATE OR REPLACE VIEW v_priority_tasks AS
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
),
design_urgency AS (
  SELECT
    CASE
      WHEN designs_30d = 0  THEN 40
      WHEN designs_30d < 25 THEN 30
      WHEN designs_30d < 50 THEN 20
      WHEN designs_30d < 75 THEN 10
      WHEN designs_30d < 90 THEN 5
      ELSE 0
    END AS bonus,
    CASE
      WHEN designs_30d < 50 THEN '🚨 Critical — ' || designs_30d || '/100 designs last 30 days'
      WHEN designs_30d < 75 THEN '⚠️ Behind — '   || designs_30d || '/100 designs last 30 days'
      WHEN designs_30d < 90 THEN '👀 Watch — '    || designs_30d || '/100 designs last 30 days'
      ELSE                       '✅ On track — '  || designs_30d || '/100 designs last 30 days'
    END AS signal
  FROM design_pace
),
email_urgency AS (
  SELECT
    CASE
      WHEN es.scheduled_count > 0 THEN 0   -- something scheduled, no urgency
      WHEN ep.emails_7d = 0       THEN 25  -- nothing sent and nothing scheduled
      WHEN ep.emails_7d < 2       THEN 10  -- only 1 sent, nothing scheduled
      ELSE 0
    END AS bonus,
    CASE
      WHEN es.scheduled_count > 0 THEN
           '✅ ' || es.scheduled_count || ' campaign(s) scheduled — next: ' ||
           TO_CHAR(es.next_scheduled AT TIME ZONE 'Europe/London', 'Dy DD Mon HH24:MI')
      WHEN ep.emails_7d = 0 THEN '🚨 No campaigns sent or scheduled this week'
      WHEN ep.emails_7d < 2 THEN '⚠️ Only ' || ep.emails_7d || ' campaign(s) this week — none scheduled'
      ELSE '✅ Email on track'
    END AS signal
  FROM email_pace ep, email_scheduled es
),
ads_urgency AS (
  SELECT
    CASE
      WHEN new_ads_7d = 0 THEN 25
      WHEN new_ads_7d < 3 THEN 10
      ELSE 0
    END AS bonus,
    CASE
      WHEN new_ads_7d = 0 THEN '🚨 No new ads launched this week'
      WHEN new_ads_7d < 3 THEN '⚠️ Only ' || new_ads_7d || ' new ad(s) this week'
      ELSE '✅ Ads on track'
    END AS signal
  FROM ads_pace
),
scored AS (
  SELECT
    t.task_id, t.title, t.category, t.priority, t.impact, t.task_type, t.effort,
    t.due_date, t.status, t.notes,
    ( CASE t.priority
        WHEN 'Low' THEN 10 WHEN 'Medium' THEN 25
        WHEN 'High' THEN 40 WHEN 'Critical' THEN 50 ELSE 0 END
    + CASE t.impact
        WHEN 'Nothing breaks if it waits'     THEN 0
        WHEN 'Creates drag / slows growth'    THEN 10
        WHEN 'Blocks progress or momentum'    THEN 20
        WHEN 'Causes real problems or losses' THEN 30 ELSE 0 END
    + CASE t.task_type
        WHEN 'Maintenance' THEN 10 WHEN 'Improvement' THEN 20
        WHEN 'Growth'      THEN 30 ELSE 0 END
    - CASE t.effort
        WHEN '< 30 minutes' THEN 0  WHEN '1-2 hours' THEN 5
        WHEN 'Half day'     THEN 10 WHEN 'Multi-day' THEN 20 ELSE 0 END
    + CASE
        WHEN t.due_date <  CURRENT_DATE     THEN 30
        WHEN t.due_date =  CURRENT_DATE     THEN 30
        WHEN t.due_date =  CURRENT_DATE + 1 THEN 20
        WHEN t.due_date <= CURRENT_DATE + 3 THEN 10
        WHEN t.due_date <= CURRENT_DATE + 7 THEN 5
        ELSE 0 END
    + CASE t.category
        WHEN 'Design' THEN (SELECT bonus FROM design_urgency)
        WHEN 'Email'  THEN (SELECT bonus FROM email_urgency)
        WHEN 'Ads'    THEN (SELECT bonus FROM ads_urgency)
        ELSE 0 END
    ) AS score,
    CASE t.category
      WHEN 'Design' THEN (SELECT signal FROM design_urgency)
      WHEN 'Email'  THEN (SELECT signal FROM email_urgency)
      WHEN 'Ads'    THEN (SELECT signal FROM ads_urgency)
      ELSE NULL
    END AS category_signal,
    CASE
      WHEN t.due_date <  CURRENT_DATE     THEN '🔴 Overdue'
      WHEN t.due_date =  CURRENT_DATE     THEN '🔴 Due today'
      WHEN t.due_date =  CURRENT_DATE + 1 THEN '🟡 Due tomorrow'
      WHEN t.due_date <= CURRENT_DATE + 3 THEN '🟡 Due in ' || (t.due_date - CURRENT_DATE) || ' days'
      WHEN t.due_date <= CURRENT_DATE + 7 THEN '🟢 Due in ' || (t.due_date - CURRENT_DATE) || ' days'
      ELSE                                     '⚪ Due ' || TO_CHAR(t.due_date, 'DD Mon')
    END AS due_label
  FROM tasks t
  WHERE t.brand_id = 'your_brand_id' AND t.status = 'Active'
)
SELECT
  task_id, title, category, priority, task_type, effort,
  due_label AS "Due", score, category_signal AS "Signal", notes,
  due_date, impact
FROM scored
ORDER BY score DESC;
