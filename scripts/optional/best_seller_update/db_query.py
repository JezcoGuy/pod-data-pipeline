"""
db_query.py
===========
Postgres helpers for best_seller_sync (v2).

- get_qualifying_products(): returns products that should carry the
  `best_seller` tag, in storefront-ranking order (30-day units DESC).
  Same query as the Metabase Product Performance dashboard.
- record_sync_run(): inserts one row into best_seller_sync_runs after
  every successful --execute. Read by nightly_alert.check_best_seller_sync.

Uses global .env credentials.
"""

import json
import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()


def _connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "your_db_name"),
        user=os.getenv("DB_USER", "your_brand_id"),
        password=os.getenv("DB_PASSWORD"),
    )


QUERY = """
WITH product_base AS (
  SELECT DISTINCT ON (product_id)
    product_id,
    TRIM(REGEXP_REPLACE(product_title, '\\s+T-[Ss]hirt$', '', 'g')) AS design,
    (CURRENT_DATE - product_created_at::date) AS days_live
  FROM product_catalogue
  WHERE brand_id = 'your_brand_id'
    AND active = true
  ORDER BY product_id
),
period_sales AS (
  SELECT
    product_id,
    SUM(CASE WHEN order_created_at >= NOW() - INTERVAL '30 days' THEN quantity ELSE 0 END) AS u30,
    SUM(CASE WHEN order_created_at >= NOW() - INTERVAL '60 days'
             AND order_created_at < NOW() - INTERVAL '30 days' THEN quantity ELSE 0 END) AS u30_prior,
    SUM(CASE WHEN order_created_at >= NOW() - INTERVAL '90 days' THEN quantity ELSE 0 END) AS u90,
    SUM(CASE WHEN order_created_at >= NOW() - INTERVAL '7 days'  THEN quantity ELSE 0 END) AS u7,
    SUM(quantity) AS u_life
  FROM v_variant_sales
  WHERE brand_id = 'your_brand_id'
  GROUP BY product_id
),
current_rank AS (
  SELECT product_id,
         RANK() OVER (ORDER BY SUM(CASE WHEN order_created_at >= NOW() - INTERVAL '30 days'
                                        THEN quantity ELSE 0 END) DESC) AS rank_now
  FROM v_variant_sales WHERE brand_id = 'your_brand_id' GROUP BY product_id
),
prior_rank AS (
  SELECT product_id,
         RANK() OVER (ORDER BY SUM(CASE WHEN order_created_at >= NOW() - INTERVAL '60 days'
                                         AND order_created_at < NOW() - INTERVAL '30 days'
                                        THEN quantity ELSE 0 END) DESC) AS rank_prior
  FROM v_variant_sales WHERE brand_id = 'your_brand_id' GROUP BY product_id
),
combined AS (
  SELECT
    pb.product_id, pb.design, pb.days_live,
    COALESCE(ps.u30, 0)       AS u30,
    COALESCE(ps.u30_prior, 0) AS u30_prior,
    COALESCE(ps.u_life, 0)    AS u_life,
    ROUND((COALESCE(ps.u7, 0)::numeric / 7.0), 3) AS recent_vel,
    ROUND((COALESCE(ps.u90, 0)::numeric / NULLIF(LEAST(pb.days_live, 90), 0)), 3) AS overall_vel,
    cr.rank_now, pr.rank_prior
  FROM product_base pb
  LEFT JOIN period_sales ps ON ps.product_id = pb.product_id
  LEFT JOIN current_rank cr ON cr.product_id = pb.product_id
  LEFT JOIN prior_rank  pr ON pr.product_id = pb.product_id
  WHERE COALESCE(ps.u_life, 0) >= 3
    AND COALESCE(ps.u30, 0)   >  0
)
SELECT
  product_id, design, u30, u30_prior, u_life,
  CASE
    WHEN u30 >= 5 AND recent_vel >  overall_vel * 1.5 THEN 'Accelerating'
    WHEN u30 >= 5 AND recent_vel >= overall_vel * 0.8 THEN 'Steady'
    WHEN u30 >= 5 AND recent_vel <  overall_vel * 0.8 THEN 'Declining'
    WHEN u30_prior = 0 AND u30 >= 3 THEN 'New Entry'
    ELSE 'Early'
  END AS momentum,
  CASE
    WHEN u30_prior = 0 AND u30 > 0 THEN 'NEW'
    WHEN rank_now < rank_prior THEN 'UP '   || (rank_prior - rank_now)::text
    WHEN rank_now > rank_prior THEN 'DOWN ' || (rank_now - rank_prior)::text
    ELSE 'SAME'
  END AS rank_change
FROM combined
ORDER BY u30 DESC;
"""


def get_qualifying_products():
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(QUERY)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    results = []
    for row in rows:
        record = dict(zip(columns, row))
        record["product_id"] = str(record["product_id"])
        results.append(record)
    return results


def get_designs_for_product_ids(product_ids):
    """
    Map product_id -> design (title with trailing T-Shirt stripped) for the
    given ids. Used to get human-readable names for products being REMOVED
    from the tag (they're no longer in the qualifying-products list).
    """
    if not product_ids:
        return {}
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (product_id)
                product_id,
                TRIM(REGEXP_REPLACE(product_title, '\\s+T-[Ss]hirt$', '', 'g')) AS design
            FROM product_catalogue
            WHERE brand_id = 'your_brand_id'
              AND product_id = ANY(%s)
            ORDER BY product_id, synced_at DESC NULLS LAST;
            """,
            (list(product_ids),),
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    return {pid: design for pid, design in rows}


def record_sync_run(brand_id, total_qualifying, total_currently_tagged,
                    added_products, removed_products, keeps_count, mode="execute"):
    """
    Append one row to best_seller_sync_runs.

    added_products / removed_products: lists of {product_id, design} dicts.
    Returns the new row id.
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO best_seller_sync_runs (
                brand_id, mode, total_qualifying, total_currently_tagged,
                adds_count, removes_count, keeps_count,
                added_products, removed_products
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            RETURNING id;
            """,
            (
                brand_id, mode, total_qualifying, total_currently_tagged,
                len(added_products), len(removed_products), keeps_count,
                json.dumps(added_products), json.dumps(removed_products),
            ),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return new_id
    finally:
        conn.close()
