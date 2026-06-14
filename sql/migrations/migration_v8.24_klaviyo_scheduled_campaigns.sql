-- Migration v8.24 — klaviyo_scheduled_campaigns
-- -----------------------------------------------
-- Storage for upcoming/scheduled email campaigns. The existing
-- email_campaigns table is reporting-style (one row per day per campaign
-- with stats AFTER send); this new table is forward-looking — fed by the
-- Klaviyo Campaigns API filtered to status in ('scheduled', 'draft') and
-- swept on every sync so cancelled/sent campaigns vanish from it.
--
-- Powers the v_priority_tasks email_urgency signal (added in v8.25): if
-- a campaign is scheduled in the next 7 days, the email-task urgency
-- bonus drops to 0 and the signal flips to a green "campaign scheduled"
-- note instead of a red "no campaigns this week" warning.

CREATE TABLE IF NOT EXISTS klaviyo_scheduled_campaigns (
    id                  SERIAL       PRIMARY KEY,
    brand_id            VARCHAR(64)  NOT NULL DEFAULT 'your_brand_id',
    campaign_id         VARCHAR(128) NOT NULL,
    campaign_name       VARCHAR(512),
    status              VARCHAR(32),
    scheduled_at        TIMESTAMPTZ,
    send_time_is_local  BOOLEAN,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    synced_at           TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (campaign_id, brand_id)
);

CREATE INDEX IF NOT EXISTS idx_klaviyo_scheduled_campaigns_scheduled_at
    ON klaviyo_scheduled_campaigns (scheduled_at)
    WHERE scheduled_at IS NOT NULL;
