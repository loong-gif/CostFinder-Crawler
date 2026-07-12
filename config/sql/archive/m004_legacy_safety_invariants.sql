-- M004: production safety invariants and durable notification primitives.
-- Dry-run and inspect production schema before applying. No destructive operations.

CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('preflight', 'started', 'applied', 'failed')),
    preflight_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    approval_token_hash TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_schema_migrations_status
    ON schema_migrations (status, started_at DESC);

ALTER TABLE promo_monitor_state
    ADD COLUMN IF NOT EXISTS cursor_created_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cursor_sequence BIGINT,
    ADD COLUMN IF NOT EXISTS lease_owner UUID,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cursor_version BIGINT NOT NULL DEFAULT 0;

ALTER TABLE promo_offer_master
    ADD COLUMN IF NOT EXISTS lifecycle_status TEXT;

UPDATE promo_offer_master
SET lifecycle_status = CASE
    WHEN status = 'ended' THEN 'ended'
    WHEN lifecycle_status IN ('active', 'needs_review', 'ended') THEN lifecycle_status
    ELSE 'active'
END
WHERE lifecycle_status IS NULL OR lifecycle_status NOT IN ('active', 'needs_review', 'ended');

ALTER TABLE promo_offer_master
    ALTER COLUMN lifecycle_status SET DEFAULT 'active',
    ALTER COLUMN lifecycle_status SET NOT NULL;

ALTER TABLE promo_offer_master
    DROP CONSTRAINT IF EXISTS promo_offer_master_lifecycle_status_check;
ALTER TABLE promo_offer_master
    ADD CONSTRAINT promo_offer_master_lifecycle_status_check
    CHECK (lifecycle_status IN ('active', 'needs_review', 'ended'));

DROP INDEX IF EXISTS idx_promo_offer_master_active_fingerprint;
CREATE UNIQUE INDEX IF NOT EXISTS idx_promo_offer_master_active_fingerprint
    ON promo_offer_master (offer_fingerprint)
    WHERE offer_fingerprint IS NOT NULL AND lifecycle_status IN ('active', 'needs_review');

ALTER TABLE promo_offer_change_events
    ADD COLUMN IF NOT EXISTS applying_lease_owner UUID,
    ADD COLUMN IF NOT EXISTS applying_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS mutation_key TEXT;

ALTER TABLE promo_offer_change_events
    DROP CONSTRAINT IF EXISTS promo_offer_change_events_validator_status_check;
ALTER TABLE promo_offer_change_events
    ADD CONSTRAINT promo_offer_change_events_validator_status_check
    CHECK (validator_status IN ('pending', 'applying', 'auto_apply', 'applied', 'needs_review', 'rejected'));

CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_offer_change_events_mutation
    ON promo_offer_change_events (change_event_id, mutation_key)
    WHERE mutation_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS operation_runs (
    run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'production',
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'partial')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    errors JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    notification_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES operation_runs(run_id) ON DELETE SET NULL,
    notification_type TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    target TEXT NOT NULL,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'sent', 'retry', 'dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner UUID,
    lease_expires_at TIMESTAMPTZ,
    provider_message_id TEXT,
    provider_request_id TEXT,
    last_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_outbox_run_type_hash
    ON notification_outbox (run_id, notification_type, payload_hash)
    WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notification_outbox_due
    ON notification_outbox (status, next_attempt_at, created_at);

CREATE OR REPLACE FUNCTION prevent_notification_payload_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.notification_id <> OLD.notification_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.notification_type <> OLD.notification_type
       OR NEW.severity <> OLD.severity
       OR NEW.target <> OLD.target
       OR NEW.payload <> OLD.payload
       OR NEW.payload_hash <> OLD.payload_hash
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'notification payload snapshot is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_notification_payload_immutable ON notification_outbox;
CREATE TRIGGER trg_notification_payload_immutable
BEFORE UPDATE ON notification_outbox
FOR EACH ROW EXECUTE FUNCTION prevent_notification_payload_mutation();

COMMENT ON TABLE notification_outbox IS
    'Durable, redacted notification snapshots. Only delivery state fields are mutable.';
COMMENT ON TABLE operation_runs IS
    'Generic run-level audit for active crawler and validator jobs.';

-- RPC-only delivery state transitions. The worker role should receive EXECUTE
-- on these functions and no direct write grants on business/outbox tables.
CREATE OR REPLACE FUNCTION claim_notification_outbox(
    p_now TIMESTAMPTZ DEFAULT NOW(),
    p_limit INTEGER DEFAULT 20
)
RETURNS SETOF notification_outbox
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    WITH due AS (
        SELECT notification_id
        FROM notification_outbox
        WHERE (status IN ('pending', 'retry') AND next_attempt_at <= p_now)
           OR (status = 'processing' AND lease_expires_at < p_now)
        ORDER BY next_attempt_at, created_at
        FOR UPDATE SKIP LOCKED
        LIMIT GREATEST(1, LEAST(p_limit, 100))
    )
    UPDATE notification_outbox n
       SET status = 'processing',
           lease_owner = gen_random_uuid(),
           lease_expires_at = p_now + INTERVAL '2 minutes',
           attempt_count = n.attempt_count + 1,
           last_attempt_at = p_now,
           updated_at = p_now
      FROM due
     WHERE n.notification_id = due.notification_id
    RETURNING n.*;
END;
$$;

CREATE OR REPLACE FUNCTION mark_notification_sent(
    p_notification_id UUID,
    p_provider_message_id TEXT,
    p_provider_request_id TEXT,
    p_delivered_at TIMESTAMPTZ DEFAULT NOW()
)
RETURNS VOID LANGUAGE SQL SECURITY DEFINER AS $$
    UPDATE notification_outbox
       SET status = 'sent',
           provider_message_id = p_provider_message_id,
           provider_request_id = p_provider_request_id,
           lease_owner = NULL,
           lease_expires_at = NULL,
           last_error = NULL,
           updated_at = p_delivered_at
     WHERE notification_id = p_notification_id;
$$;

CREATE OR REPLACE FUNCTION mark_notification_retry(
    p_notification_id UUID,
    p_error TEXT,
    p_next_attempt_at TIMESTAMPTZ,
    p_delivered_at TIMESTAMPTZ DEFAULT NOW()
)
RETURNS VOID LANGUAGE SQL SECURITY DEFINER AS $$
    UPDATE notification_outbox
       SET status = CASE WHEN attempt_count >= 5 THEN 'dead_letter' ELSE 'retry' END,
           next_attempt_at = p_next_attempt_at,
           lease_owner = NULL,
           lease_expires_at = NULL,
           last_error = LEFT(p_error, 1000),
           updated_at = p_delivered_at
     WHERE notification_id = p_notification_id;
$$;

CREATE OR REPLACE FUNCTION mark_notification_dead_letter(
    p_notification_id UUID,
    p_error TEXT,
    p_delivered_at TIMESTAMPTZ DEFAULT NOW()
)
RETURNS VOID LANGUAGE SQL SECURITY DEFINER AS $$
    UPDATE notification_outbox
       SET status = 'dead_letter',
           lease_owner = NULL,
           lease_expires_at = NULL,
           last_error = LEFT(p_error, 1000),
           updated_at = p_delivered_at
     WHERE notification_id = p_notification_id;
$$;
