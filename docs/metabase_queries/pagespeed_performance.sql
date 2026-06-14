-- PageSpeed Performance
-- Card ID: 65
-- Collection: Root
-- Updated: 2026-06-06T15:12:56.021194Z
-- Extracted: 2026-06-14T10:36:25Z

SELECT
  l.page_path AS "Page",
  l.strategy AS "Strategy",
  l.date :: date AS "Date",
  l.score_performance AS "Performance",
  ROUND(AVG(h.score_performance) :: numeric, 1) AS "7d Avg",
  l.score_seo AS "SEO",
  l.score_accessibility AS "Accessibility",
  l.score_best_practices AS "Best Practices",
  CASE
    WHEN l.score_performance >= 90 THEN '🟢'
    WHEN l.score_performance >= 70 THEN '🟡'
    ELSE '🔴'
  END AS "Status"
FROM
  pagespeed_daily l
  JOIN pagespeed_daily h ON h.page_path = l.page_path
  AND h.strategy = l.strategy
  AND h.brand_id = l.brand_id
  AND h.date >= l.date - INTERVAL '7 days'
  AND h.date <= l.date
WHERE
  l.brand_id = 'your_brand_id'
  AND l.date = (
    SELECT
      MAX(date)
    FROM
      pagespeed_daily
    WHERE
      brand_id = 'your_brand_id'
  )
GROUP BY
  l.page_path,
  l.strategy,
  l.date,
  l.score_performance,
  l.score_seo,
  l.score_accessibility,
  l.score_best_practices
ORDER BY
  l.page_path,
  l.strategy;
