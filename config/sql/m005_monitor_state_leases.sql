-- M005: additive monitor lease fields for the verified existing table.
-- Preconditions: public.promo_monitor_state exists with monitor_id primary key.

DO $$
BEGIN
    IF to_regclass('public.promo_monitor_state') IS NULL THEN
        RAISE EXCEPTION 'M005 requires public.promo_monitor_state; run the existing monitor-state migration first';
    END IF;
END $$;

ALTER TABLE public.promo_monitor_state
    ADD COLUMN IF NOT EXISTS cursor_created_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cursor_sequence BIGINT,
    ADD COLUMN IF NOT EXISTS lease_owner UUID,
    ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cursor_version BIGINT NOT NULL DEFAULT 0;
