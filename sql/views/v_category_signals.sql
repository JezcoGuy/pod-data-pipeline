-- View: v_category_signals
-- Extracted from live DB via pg_get_viewdef(). Re-create with:
--   psql ... -f v_category_signals.sql

CREATE OR REPLACE VIEW v_category_signals AS
 WITH design_pace AS (
         SELECT count(DISTINCT product_catalogue.product_handle) AS designs_30d
           FROM product_catalogue
          WHERE product_catalogue.brand_id::text = 'your_brand_id'::text AND product_catalogue.product_created_at >= (now() - '30 days'::interval)
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
        )
 SELECT 'Design'::text AS category,
    1 AS sort_order,
        CASE
            WHEN design_pace.designs_30d < 50 THEN 'crit'::text
            WHEN design_pace.designs_30d < 75 THEN 'warn'::text
            WHEN design_pace.designs_30d < 90 THEN 'watch'::text
            ELSE 'ok'::text
        END AS level,
        CASE
            WHEN design_pace.designs_30d < 50 THEN ('🚨 Critical — '::text || design_pace.designs_30d) || '/100 designs last 30 days'::text
            WHEN design_pace.designs_30d < 75 THEN ('⚠️ Behind — '::text || design_pace.designs_30d) || '/100 designs last 30 days'::text
            WHEN design_pace.designs_30d < 90 THEN ('👀 Watch — '::text || design_pace.designs_30d) || '/100 designs last 30 days'::text
            ELSE ('✅ On track — '::text || design_pace.designs_30d) || '/100 designs last 30 days'::text
        END AS signal
   FROM design_pace
UNION ALL
 SELECT 'Ads'::text AS category,
    2 AS sort_order,
        CASE
            WHEN ads_pace.new_ads_7d = 0 THEN 'crit'::text
            WHEN ads_pace.new_ads_7d < 3 THEN 'warn'::text
            ELSE 'ok'::text
        END AS level,
        CASE
            WHEN ads_pace.new_ads_7d = 0 THEN '🚨 No new ads launched this week'::text
            WHEN ads_pace.new_ads_7d < 3 THEN ('⚠️ Only '::text || ads_pace.new_ads_7d) || ' new ad(s) this week'::text
            ELSE '✅ Ads on track'::text
        END AS signal
   FROM ads_pace
UNION ALL
 SELECT 'Email'::text AS category,
    3 AS sort_order,
        CASE
            WHEN es.scheduled_count > 0 THEN 'ok'::text
            ELSE 'crit'::text
        END AS level,
        CASE
            WHEN es.scheduled_count > 0 THEN (('✅ '::text || es.scheduled_count) || ' campaign(s) scheduled — next: '::text) || to_char((es.next_scheduled AT TIME ZONE 'Europe/London'::text), 'Dy DD Mon HH24:MI'::text)
            ELSE '🚨 No campaigns scheduled — time to queue one'::text
        END AS signal
   FROM email_scheduled es
  ORDER BY 2;
