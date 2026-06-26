import os
import smtplib
from decimal import Decimal
from email.message import EmailMessage

import psycopg2
from dotenv import load_dotenv
from fastapi import Body, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# Pull TASK_PIN (and anything else) from the shared project .env. DB_PASSWORD
# is set via systemd Environment= and overrides; load_dotenv() leaves
# already-set env vars alone by default.
load_dotenv()  # picks up .env from the working directory

app = FastAPI()
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://your-domain.com",
        "https://www.your-domain.com",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "database": os.getenv("DB_NAME"),
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}

# Shared secret for /tasks/add and /time-log/add. Read endpoints (priority,
# category-signals) and existing-task mutations (update, edit) are NOT
# gated — only the two create endpoints. If TASK_PIN is unset or empty,
# the check fails closed (every submission gets 401) so a missing config
# doesn't silently disable PIN protection.
TASK_PIN = os.getenv("TASK_PIN")


def _check_pin(body):
    """Return a 401 JSONResponse if the body's pin doesn't match TASK_PIN,
    else None. Used at the top of the protected endpoints."""
    if not TASK_PIN or (body.get("pin") or "") != TASK_PIN:
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "Incorrect PIN"},
        )
    return None


@app.get("/snapshot")
@limiter.limit("20/minute")
def get_snapshot(request: Request):
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        SELECT
            shopify_orders_today,
            shopify_items_today,
            ROUND(shopify_revenue_today_gbp::numeric, 2),
            ROUND(shopify_aov_today_gbp::numeric, 2),
            ROUND(meta_spend_today_gbp::numeric, 2),
            ROUND(mer::numeric, 2),
            ROUND(((shopify_revenue_today_gbp * 0.579) - meta_spend_today_gbp)::numeric, 2),
            snapshot_at
        FROM live_snapshot
        WHERE brand_id = 'your_brand_id'
        ORDER BY snapshot_at DESC
        LIMIT 1;
    """)
    row = cur.fetchone()
    cur.close()
    conn.close()
    return {
        "orders": row[0],
        "items": row[1],
        "revenue": float(row[2]),
        "aov": float(row[3]),
        "ad_spend": float(row[4]),
        "mer": float(row[5]),
        "est_net": float(row[6]),
        "snapshot_at": row[7].strftime("%d %b %Y %H:%M") if row[7] else "Unknown",
    }


@app.get("/pl-monthly")
@limiter.limit("20/minute")
def get_pl_monthly(request: Request):
    """Current-month P&L row from v_pl_monthly (Europe/London month boundary)."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        SELECT
            TO_CHAR(month, 'FMMonth YYYY')         AS month_label,
            orders,
            ROUND(aov::numeric, 2)                 AS aov,
            ROUND(gross_revenue::numeric, 2)       AS gross_revenue,
            ROUND(refunds::numeric, 2)             AS refunds,
            ROUND(net_revenue::numeric, 2)         AS net_revenue,
            ROUND(cogs_orders::numeric, 2)         AS cogs_orders,
            ROUND(gross_profit::numeric, 2)        AS gross_profit,
            ROUND(gross_margin_pct::numeric, 1)    AS gross_margin_pct,
            ROUND(meta_spend::numeric, 2)          AS meta_spend,
            ROUND(mer::numeric, 2)                 AS mer,
            ROUND(after_meta::numeric, 2)          AS after_meta,
            ROUND(after_meta_pct::numeric, 1)      AS after_meta_pct,
            ROUND(payment_fees::numeric, 2)        AS payment_fees,
            ROUND(total_overheads::numeric, 2)     AS total_overheads,
            ROUND(operating_profit::numeric, 2)    AS operating_profit,
            ROUND(operating_margin_pct::numeric, 1) AS operating_margin_pct,
            ROUND(tax::numeric, 2)                 AS tax,
            ROUND(drawings::numeric, 2)            AS drawings,
            ROUND(net_cash::numeric, 2)            AS net_cash,
            ROUND(net_cash_pct::numeric, 1)        AS net_cash_pct,
            ROUND(monzo_net_movement::numeric, 2)  AS monzo_net_movement,
            ROUND(monzo_net_pct::numeric, 1)       AS monzo_net_pct,
            ROUND(pending_payouts::numeric, 2)     AS pending_payouts,
            ROUND(reconciliation_gap_pct::numeric, 1) AS reconciliation_gap_pct,
            NOW() AT TIME ZONE 'Europe/London'     AS generated_at
        FROM v_pl_monthly
        WHERE month = DATE_TRUNC('month', CURRENT_DATE AT TIME ZONE 'Europe/London')::date
        LIMIT 1;
    """)
    row = cur.fetchone()
    columns = [d[0] for d in cur.description]
    cur.close()
    conn.close()
    if not row:
        return {"error": "No data for current month"}
    out = {}
    for col, val in zip(columns, row):
        if isinstance(val, Decimal):
            out[col] = float(val)
        elif hasattr(val, "strftime"):  # generated_at TIMESTAMPTZ -> human string
            out[col] = val.strftime("%d %b %Y %H:%M")
        else:
            out[col] = val
    if isinstance(out.get("month_label"), str):
        out["month_label"] = out["month_label"].strip()  # TO_CHAR pads with spaces
    return out


@app.get("/health-snapshot")
@limiter.limit("20/minute")
def get_health_snapshot(request: Request):
    """7-day business-health metric tiles for the morning glance dashboard."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        WITH pl AS (
          SELECT
            ROUND(AVG(revenue / NULLIF(meta_spend,0))::numeric, 2) AS mer,
            ROUND(AVG(cogs / NULLIF(revenue,0) * 100)::numeric, 1) AS cogs_pct,
            ROUND(AVG((revenue - cogs - meta_spend - fees) /
              NULLIF(revenue,0) * 100)::numeric, 1) AS pl_pct,
            ROUND(AVG(cpc)::numeric, 2) AS cpc,
            ROUND(AVG(atc_pct)::numeric, 1) AS atc_pct
          FROM (
            SELECT
              (o.created_at AT TIME ZONE 'Europe/London')::date AS day,
              SUM(o.revenue_gbp) AS revenue,
              SUM(CASE
                WHEN o.cogs_gbp = 0
                  AND o.created_at >= NOW() - INTERVAL '48 hours'
                  AND o.financial_status NOT IN ('voided','refunded')
                THEN o.revenue_gbp * 0.421
                ELSE o.cogs_gbp
              END) AS cogs,
              SUM(o.total_payment_fees) AS fees,
              COALESCE(SUM(a.spend_gbp), 0) AS meta_spend,
              ROUND(SUM(a.spend_gbp) / NULLIF(SUM(a.clicks),0), 2) AS cpc,
              ROUND(SUM(a.add_to_cart_count)::numeric /
                NULLIF(SUM(a.clicks),0) * 100, 1) AS atc_pct
            FROM orders o
            LEFT JOIN ad_campaigns a
              ON (a.date AT TIME ZONE 'Europe/London')::date =
                 (o.created_at AT TIME ZONE 'Europe/London')::date
              AND a.brand_id = o.brand_id
            WHERE o.brand_id = 'your_brand_id'
              AND o.financial_status NOT IN ('voided','refunded')
              AND o.created_at >= NOW() - INTERVAL '7 days'
            GROUP BY 1
          ) daily
          WHERE day < CURRENT_DATE
        ),
        funnel AS (
          SELECT ROUND(AVG(cr_pct)::numeric, 2) AS cr_pct
          FROM v_sessions_daily
          WHERE brand_id = 'your_brand_id'
            AND date >= CURRENT_DATE - INTERVAL '7 days'
            AND date < CURRENT_DATE
        ),
        inputs AS (
          SELECT COUNT(DISTINCT product_handle) AS designs_this_week
          FROM product_catalogue
          WHERE brand_id = 'your_brand_id'
            AND (product_created_at AT TIME ZONE 'Europe/London')::date >= CURRENT_DATE - 7
            AND (product_created_at AT TIME ZONE 'Europe/London')::date <  CURRENT_DATE
        ),
        emails AS (
          SELECT COUNT(DISTINCT campaign_id) AS emails_this_week
          FROM email_campaigns
          WHERE brand_id = 'your_brand_id' AND campaign_type = 'campaign'
            AND date >= CURRENT_DATE - 7 AND date < CURRENT_DATE
        ),
        ads AS (
          SELECT COUNT(DISTINCT ad_id) AS ads_launched_this_week
          FROM (SELECT ad_id, MIN(date::date) AS first_seen
                FROM ad_campaigns WHERE brand_id = 'your_brand_id' GROUP BY ad_id) x
          WHERE first_seen >= CURRENT_DATE - 7 AND first_seen < CURRENT_DATE
        )
        SELECT pl.mer, pl.cogs_pct, pl.pl_pct, pl.cpc, pl.atc_pct,
               funnel.cr_pct, inputs.designs_this_week,
               emails.emails_this_week, ads.ads_launched_this_week,
               NOW() AT TIME ZONE 'Europe/London' AS generated_at
        FROM pl, funnel, inputs, emails, ads;
    """)
    row = cur.fetchone()
    columns = [d[0] for d in cur.description]
    cur.close()
    conn.close()
    if not row:
        return {"error": "No data"}
    out = {}
    for col, val in zip(columns, row):
        if isinstance(val, Decimal):
            out[col] = float(val)
        elif hasattr(val, "strftime"):
            out[col] = val.strftime("%d %b %Y %H:%M")
        else:
            out[col] = val
    return out


# ─── Task manager endpoints ───────────────────────────────────────────────────

@app.get("/tasks/priority")
@limiter.limit("30/minute")
def get_priority_tasks(request: Request):
    """Live ranked task list — reads v_priority_tasks (recomputes every query)."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        SELECT
            task_id, title, category, priority,
            impact, task_type, effort,
            due_date::text AS due_date,
            score, "Signal" AS signal,
            "Due" AS due_label, notes,
            urgency_tier
        FROM v_priority_tasks
        ORDER BY urgency_tier ASC, score DESC
    """)
    rows    = cur.fetchall()
    columns = [d[0] for d in cur.description]
    cur.close()
    conn.close()
    out = []
    for row in rows:
        d = {}
        for col, val in zip(columns, row):
            if isinstance(val, Decimal):
                d[col] = int(val) if val == int(val) else float(val)
            else:
                d[col] = val
        out.append(d)
    return out


@app.post("/tasks/add")
@limiter.limit("30/minute")
def add_task(request: Request, task: dict = Body(...)):
    """Insert a new task. Postgres CHECK constraints surface invalid values."""
    err = _check_pin(task)
    if err is not None:
        return err
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tasks (
                brand_id, title, category, priority,
                impact, task_type, effort, due_date, notes
            ) VALUES (
                'your_brand_id', %(title)s, %(category)s, %(priority)s,
                %(impact)s, %(task_type)s, %(effort)s,
                %(due_date)s, %(notes)s
            ) RETURNING task_id
        """, task)
        task_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return {"success": True, "task_id": task_id}
    except psycopg2.Error as e:
        return {"success": False, "error": str(e)}


@app.post("/tasks/update")
@limiter.limit("30/minute")
def update_task(request: Request, data: dict = Body(...)):
    """Patch status and/or due_date on an existing task."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
            UPDATE tasks
            SET status   = %(status)s,
                due_date = COALESCE(%(due_date)s::date, due_date),
                updated_at = NOW()
            WHERE task_id = %(task_id)s AND brand_id = 'your_brand_id'
        """, {
            "status":   data.get("status"),
            "due_date": data.get("due_date"),
            "task_id":  data.get("task_id"),
        })
        rc = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return {"success": True, "rows_updated": rc}
    except psycopg2.Error as e:
        return {"success": False, "error": str(e)}


@app.post("/tasks/edit")
@limiter.limit("30/minute")
def edit_task(request: Request, data: dict = Body(...)):
    """Full content edit of a task — status not touched (use /tasks/update for that)."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
            UPDATE tasks SET
                title      = %(title)s,
                category   = %(category)s,
                priority   = %(priority)s,
                impact     = %(impact)s,
                task_type  = %(task_type)s,
                effort     = %(effort)s,
                due_date   = %(due_date)s::date,
                notes      = %(notes)s,
                updated_at = NOW()
            WHERE task_id = %(task_id)s AND brand_id = 'your_brand_id'
        """, data)
        rc = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return {"success": True, "rows_updated": rc}
    except psycopg2.Error as e:
        return {"success": False, "error": str(e)}


@app.post("/time-log/add")
@limiter.limit("30/minute")
def add_time_log(request: Request, entry: dict = Body(...)):
    """Append a time_log entry."""
    err = _check_pin(entry)
    if err is not None:
        return err
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO time_log (
                brand_id, log_date, category,
                duration_minutes, activity, notes
            ) VALUES (
                'your_brand_id', %(log_date)s, %(category)s,
                %(duration_minutes)s, %(activity)s, %(notes)s
            ) RETURNING log_id
        """, entry)
        log_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return {"success": True, "log_id": log_id}
    except psycopg2.Error as e:
        return {"success": False, "error": str(e)}


@app.get("/category-signals")
@limiter.limit("30/minute")
def get_category_signals(request: Request):
    """One row per tracked category — read by the top-of-page tiles on tasks.html.
    Independent of v_priority_tasks so the bar surfaces even with zero Active tasks."""
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("SELECT category, level, signal FROM v_category_signals ORDER BY sort_order;")
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [{"category": c, "level": l, "signal": s} for (c, l, s) in rows]


# ─── EU Withdrawal Form ───────────────────────────────────────────────────────
# Public POST endpoint behind https://api.your-domain.com/withdrawal — receives
# consumer withdrawal-of-purchase requests, logs to DB (idempotent via
# request_id UNIQUE), and fires confirmation + internal SMTP notifications.

class WithdrawalRequest(BaseModel):
    request_id: str
    full_name: str
    email: str
    order_reference: str
    additional_details: str = ''
    withdrawal_statement: str = ''
    submitted_at: str
    submitted_at_local: str = ''
    page_url: str = ''


@app.post("/withdrawal")
async def submit_withdrawal(data: WithdrawalRequest, request: Request):
    client_ip = request.client.host if request.client else None

    # 1. Log to DB (idempotent on request_id)
    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO withdrawal_requests (
            brand_id, request_id, full_name, email,
            order_reference, additional_details, withdrawal_statement,
            submitted_at, submitted_at_local, page_url, ip_address
        ) VALUES (
            'your_brand_id', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        ) ON CONFLICT (request_id) DO NOTHING
    """, (
        data.request_id, data.full_name, data.email,
        data.order_reference, data.additional_details,
        data.withdrawal_statement, data.submitted_at,
        data.submitted_at_local, data.page_url,
        client_ip,
    ))
    conn.commit()
    cur.close()
    conn.close()

    # 2. Send emails
    _send_withdrawal_confirmation(data)
    _send_withdrawal_internal(data, client_ip)

    return {"success": True, "message": "Withdrawal request received"}


def _send_withdrawal_confirmation(data: WithdrawalRequest):
    first_name = data.full_name.split()[0] if data.full_name else "Customer"
    additional_block = (
        f"\n  Additional details: {data.additional_details}"
        if data.additional_details else ""
    )
    msg = EmailMessage()
    msg['Subject'] = f'Withdrawal Request Received — Order {data.order_reference}'
    msg['From']    = os.getenv('SMTP_FROM')
    msg['To']      = data.email
    msg.set_content(f"""Dear {first_name},

We confirm receipt of your withdrawal request submitted on {data.submitted_at_local or data.submitted_at}.

Your request details:
  Name:            {data.full_name}
  Email:           {data.email}
  Order reference: {data.order_reference}
  Submitted:       {data.submitted_at_local or data.submitted_at}{additional_block}

Under your rights as an EU consumer, your withdrawal has been recorded at the date and time shown above.

We will review your request and contact you within 48 business hours regarding next steps, including any return instructions and refund timeline.

If you have any questions in the meantime, please contact us at support@your-domain.com.

Kind regards,
Your Brand Customer Service
your-domain.com
""")
    _smtp_send(msg)


def _send_withdrawal_internal(data: WithdrawalRequest, client_ip: str | None):
    msg = EmailMessage()
    msg['Subject'] = f'[Withdrawal Request] {data.order_reference} — {data.full_name}'
    msg['From']    = os.getenv('SMTP_FROM')
    msg['To']      = os.getenv('SMTP_TO')
    msg.set_content(f"""New EU withdrawal request received.

Name:         {data.full_name}
Email:        {data.email}
Order:        {data.order_reference}
Submitted:    {data.submitted_at_local or data.submitted_at}
Additional:   {data.additional_details or 'None'}
Request ID:   {data.request_id}
Page URL:     {data.page_url}
IP Address:   {client_ip or 'unknown'}
""")
    _smtp_send(msg)


def _smtp_send(msg: EmailMessage):
    port = int(os.getenv('SMTP_PORT', '587'))
    host = os.getenv('SMTP_HOST')
    user = os.getenv('SMTP_USER') or os.getenv('SMTP_FROM')
    password = os.getenv('SMTP_PASS')
    if port == 465:
        # Implicit SSL
        with smtplib.SMTP_SSL(host, port) as s:
            s.login(user, password)
            s.send_message(msg)
    else:
        # STARTTLS (587 default — works on more VPS providers than 465)
        with smtplib.SMTP(host, port) as s:
            s.ehlo()
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
