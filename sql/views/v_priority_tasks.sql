-- View: v_priority_tasks
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_priority_tasks.sql

CREATE OR REPLACE VIEW v_priority_tasks AS
 WITH design_pace AS (
         SELECT count(DISTINCT product_catalogue.product_handle) AS designs_30d
           FROM product_catalogue
          WHERE product_catalogue.brand_id::text = 'your_brand_id'::text AND product_catalogue.product_created_at >= (now() - '30 days'::interval)
        ), email_pace AS (
         SELECT count(DISTINCT email_campaigns.campaign_id) AS emails_7d
           FROM email_campaigns
          WHERE email_campaigns.brand_id::text = 'your_brand_id'::text AND email_campaigns.campaign_type::text = 'campaign'::text AND email_campaigns.date >= (CURRENT_DATE - '7 days'::interval)
        ), email_scheduled AS (
         SELECT count(*) AS scheduled_count,
            min(klaviyo_scheduled_campaigns.scheduled_at) AS next_scheduled
           FROM klaviyo_scheduled_campaigns
          WHERE klaviyo_scheduled_campaigns.brand_id::text = 'your_brand_id'::text AND klaviyo_scheduled_campaigns.status::text = 'scheduled'::text AND klaviyo_scheduled_campaigns.scheduled_at >= now() AND klaviyo_scheduled_campaigns.scheduled_at <= (now() + '7 days'::interval)
        ), ads_pace AS (
         SELECT count(DISTINCT x.ad_id) AS new_ads_7d
           FROM ( SELECT ad_campaigns.ad_id,
                    min(ad_campaigns.date) AS first_seen
                   FROM ad_campaigns
                  WHERE ad_campaigns.brand_id::text = 'your_brand_id'::text
                  GROUP BY ad_campaigns.ad_id) x
          WHERE x.first_seen >= (CURRENT_DATE - 7)
        ), design_urgency AS (
         SELECT
                CASE
                    WHEN design_pace.designs_30d = 0 THEN 40
                    WHEN design_pace.designs_30d < 25 THEN 30
                    WHEN design_pace.designs_30d < 50 THEN 20
                    WHEN design_pace.designs_30d < 75 THEN 10
                    WHEN design_pace.designs_30d < 90 THEN 5
                    ELSE 0
                END AS bonus,
                CASE
                    WHEN design_pace.designs_30d < 50 THEN ('🚨 Critical — '::text || design_pace.designs_30d) || '/100 designs last 30 days'::text
                    WHEN design_pace.designs_30d < 75 THEN ('⚠️ Behind — '::text || design_pace.designs_30d) || '/100 designs last 30 days'::text
                    WHEN design_pace.designs_30d < 90 THEN ('👀 Watch — '::text || design_pace.designs_30d) || '/100 designs last 30 days'::text
                    ELSE ('✅ On track — '::text || design_pace.designs_30d) || '/100 designs last 30 days'::text
                END AS signal
           FROM design_pace
        ), email_urgency AS (
         SELECT
                CASE
                    WHEN es.scheduled_count > 0 THEN 0
                    WHEN ep.emails_7d = 0 THEN 25
                    WHEN ep.emails_7d < 2 THEN 10
                    ELSE 0
                END AS bonus,
                CASE
                    WHEN es.scheduled_count > 0 THEN (('✅ '::text || es.scheduled_count) || ' campaign(s) scheduled — next: '::text) || to_char((es.next_scheduled AT TIME ZONE 'Europe/London'::text), 'Dy DD Mon HH24:MI'::text)
                    WHEN ep.emails_7d = 0 THEN '🚨 No campaigns sent or scheduled this week'::text
                    WHEN ep.emails_7d < 2 THEN ('⚠️ Only '::text || ep.emails_7d) || ' campaign(s) this week — none scheduled'::text
                    ELSE '✅ Email on track'::text
                END AS signal
           FROM email_pace ep,
            email_scheduled es
        ), ads_urgency AS (
         SELECT
                CASE
                    WHEN ads_pace.new_ads_7d = 0 THEN 25
                    WHEN ads_pace.new_ads_7d < 3 THEN 10
                    ELSE 0
                END AS bonus,
                CASE
                    WHEN ads_pace.new_ads_7d = 0 THEN '🚨 No new ads launched this week'::text
                    WHEN ads_pace.new_ads_7d < 3 THEN ('⚠️ Only '::text || ads_pace.new_ads_7d) || ' new ad(s) this week'::text
                    ELSE '✅ Ads on track'::text
                END AS signal
           FROM ads_pace
        ), scored AS (
         SELECT t.task_id,
            t.title,
            t.category,
            t.priority,
            t.impact,
            t.task_type,
            t.effort,
            t.due_date,
            t.status,
            t.notes,
                CASE t.priority
                    WHEN 'Low'::text THEN 10
                    WHEN 'Medium'::text THEN 25
                    WHEN 'High'::text THEN 40
                    WHEN 'Critical'::text THEN 50
                    ELSE 0
                END +
                CASE t.impact
                    WHEN 'Nothing breaks if it waits'::text THEN 0
                    WHEN 'Creates drag / slows growth'::text THEN 10
                    WHEN 'Blocks progress or momentum'::text THEN 20
                    WHEN 'Causes real problems or losses'::text THEN 30
                    ELSE 0
                END +
                CASE t.task_type
                    WHEN 'Maintenance'::text THEN 10
                    WHEN 'Improvement'::text THEN 20
                    WHEN 'Growth'::text THEN 30
                    ELSE 0
                END -
                CASE t.effort
                    WHEN '< 30 minutes'::text THEN 0
                    WHEN '1-2 hours'::text THEN 5
                    WHEN 'Half day'::text THEN 10
                    WHEN 'Multi-day'::text THEN 20
                    ELSE 0
                END +
                CASE
                    WHEN t.due_date < CURRENT_DATE THEN 30
                    WHEN t.due_date = CURRENT_DATE THEN 30
                    WHEN t.due_date = (CURRENT_DATE + 1) THEN 20
                    WHEN t.due_date <= (CURRENT_DATE + 3) THEN 10
                    WHEN t.due_date <= (CURRENT_DATE + 7) THEN 5
                    ELSE 0
                END +
                CASE t.category
                    WHEN 'Design'::text THEN ( SELECT design_urgency.bonus
                       FROM design_urgency)
                    WHEN 'Email'::text THEN ( SELECT email_urgency.bonus
                       FROM email_urgency)
                    WHEN 'Ads'::text THEN ( SELECT ads_urgency.bonus
                       FROM ads_urgency)
                    ELSE 0
                END AS score,
                CASE t.category
                    WHEN 'Design'::text THEN ( SELECT design_urgency.signal
                       FROM design_urgency)
                    WHEN 'Email'::text THEN ( SELECT email_urgency.signal
                       FROM email_urgency)
                    WHEN 'Ads'::text THEN ( SELECT ads_urgency.signal
                       FROM ads_urgency)
                    ELSE NULL::text
                END AS category_signal,
                CASE
                    WHEN t.due_date < CURRENT_DATE THEN '🔴 Overdue'::text
                    WHEN t.due_date = CURRENT_DATE THEN '🔴 Due today'::text
                    WHEN t.due_date = (CURRENT_DATE + 1) THEN '🟡 Due tomorrow'::text
                    WHEN t.due_date <= (CURRENT_DATE + 3) THEN ('🟡 Due in '::text || (t.due_date - CURRENT_DATE)) || ' days'::text
                    WHEN t.due_date <= (CURRENT_DATE + 7) THEN ('🟢 Due in '::text || (t.due_date - CURRENT_DATE)) || ' days'::text
                    ELSE '⚪ Due '::text || to_char(t.due_date::timestamp with time zone, 'DD Mon'::text)
                END AS due_label,
                CASE
                    WHEN t.due_date < CURRENT_DATE THEN 1
                    WHEN t.due_date = CURRENT_DATE THEN 1
                    WHEN t.due_date = (CURRENT_DATE + 1) THEN 2
                    WHEN t.due_date <= (CURRENT_DATE + 3) THEN 2
                    WHEN t.due_date <= (CURRENT_DATE + 7) THEN 3
                    ELSE 4
                END AS urgency_tier
           FROM tasks t
          WHERE t.brand_id::text = 'your_brand_id'::text AND t.status::text = 'Active'::text
        )
 SELECT task_id,
    title,
    category,
    priority,
    task_type,
    effort,
    due_label AS "Due",
    score,
    category_signal AS "Signal",
    notes,
    due_date,
    impact,
    urgency_tier
   FROM scored
  ORDER BY urgency_tier, score DESC;
