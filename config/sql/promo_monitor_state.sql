-- Firecrawl monitor polling state for change-gated recrawl.
-- Run manually in Supabase SQL editor before using firecrawl_monitor_poll.py.

CREATE TABLE IF NOT EXISTS promo_monitor_state (
    monitor_id TEXT PRIMARY KEY,
    domain_name TEXT NOT NULL,
    last_check_id TEXT,
    last_change_at TIMESTAMPTZ,
    last_processed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_promo_monitor_state_domain
    ON promo_monitor_state (domain_name);

COMMENT ON TABLE promo_monitor_state IS
    'Tracks last processed Firecrawl monitor check per monitor_id for idempotent polling.';
