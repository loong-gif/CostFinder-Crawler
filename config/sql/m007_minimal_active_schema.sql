-- M007: minimum schema required by the currently active crawler and
-- change-driven extraction paths.
--
-- This intentionally excludes historical data repairs, lifecycle cutover,
-- monitor leases, notification outbox, evidence segments, membership-plan
-- storage, product storage, and destructive column drops.  Those features
-- either have no active writer/reader or require a separately reviewed
-- migration and data preflight.
--
-- Prerequisite: the legacy base tables promo_offer_master and
-- promo_website_staging already exist.  Run with scripts/apply_sql_migration.py
-- so the migration ledger and checksum guard are used.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.promo_offer_master') IS NULL THEN
        RAISE EXCEPTION 'M007 requires public.promo_offer_master';
    END IF;
    IF to_regclass('public.promo_website_staging') IS NULL THEN
        RAISE EXCEPTION 'M007 requires public.promo_website_staging';
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'promo_offer_master'
          AND column_name = 'status'
    ) THEN
        RAISE EXCEPTION 'M007 requires promo_offer_master.status for active-offer uniqueness';
    END IF;
END $$;

-- UUID defaults used by the change-event tables.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Current offer writers persist and query this deterministic fingerprint.
ALTER TABLE promo_offer_master
    ADD COLUMN IF NOT EXISTS offer_fingerprint TEXT;

-- staging_recrawl always writes this flag when it upserts a crawled page.
ALTER TABLE promo_website_staging
    ADD COLUMN IF NOT EXISTS is_membership_page BOOLEAN NOT NULL DEFAULT FALSE;

-- The active change-driven path persists only change events and their selected
-- candidates.  crawl_run_id and segment_id remain UUID values without foreign
-- keys because the corresponding crawl/segment tables have no active writer.
CREATE TABLE IF NOT EXISTS promo_offer_change_events (
    change_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promo_website_id BIGINT,
    source_url TEXT NOT NULL,
    source_url_normalized TEXT NOT NULL,
    business_id BIGINT,
    crawl_run_id UUID,
    monitor_event_id TEXT,
    diff_type TEXT,
    business_change_type TEXT NOT NULL DEFAULT 'unknown',
    affected_segment_ids UUID[] NOT NULL DEFAULT '{}',
    before_text TEXT,
    after_text TEXT,
    before_hash TEXT,
    after_hash TEXT,
    proposed_action TEXT NOT NULL,
    target_offer_id BIGINT,
    proposed_field_updates JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposed_new_offer JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence NUMERIC,
    confidence_label TEXT,
    reason TEXT,
    validator_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (validator_status IN ('pending', 'applying', 'auto_apply', 'applied', 'needs_review', 'rejected')),
    validator_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    applied_at TIMESTAMPTZ,
    applied_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS promo_offer_match_candidates (
    match_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    change_event_id UUID NOT NULL REFERENCES promo_offer_change_events(change_event_id) ON DELETE CASCADE,
    segment_id UUID,
    candidate_offer_id BIGINT,
    match_score NUMERIC NOT NULL,
    match_method TEXT NOT NULL,
    score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    rank INTEGER,
    is_selected BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Database-level protection against concurrent duplicate active-offer writes.
-- The migration rolls back without changing the schema if historical active
-- duplicates remain; deduplicate them before retrying.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM promo_offer_master
        WHERE status = 'active'
          AND offer_fingerprint IS NOT NULL
        GROUP BY offer_fingerprint
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'M007 requires active offer_fingerprint values to be deduplicated before creating the unique index';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_promo_offer_master_active_fp
    ON promo_offer_master (offer_fingerprint)
    WHERE offer_fingerprint IS NOT NULL AND status = 'active';

COMMIT;
