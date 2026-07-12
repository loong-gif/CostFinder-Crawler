-- M006: additive evidence/change-event tables for the verified legacy offer schema.
-- This migration deliberately does NOT alter promo_offer_master.status or add
-- lifecycle_status. That compatibility decision requires a separate backfill.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    IF to_regclass('public.promo_offer_master') IS NULL
       OR to_regclass('public.promo_website_staging') IS NULL THEN
        RAISE EXCEPTION 'M006 requires promo_offer_master and promo_website_staging';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS promo_crawl_runs (
    crawl_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promo_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE SET NULL,
    source_url TEXT NOT NULL,
    source_url_normalized TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'success',
    content_hash TEXT,
    content_quality_score NUMERIC,
    content_quality_state TEXT NOT NULL DEFAULT 'ok',
    error TEXT,
    raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS promo_page_segments (
    segment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promo_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE CASCADE,
    crawl_run_id UUID REFERENCES promo_crawl_runs(crawl_run_id) ON DELETE SET NULL,
    business_id BIGINT REFERENCES master_business_info(business_id) ON DELETE SET NULL,
    source_url TEXT NOT NULL,
    source_url_normalized TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    segment_type TEXT NOT NULL DEFAULT 'unknown',
    heading_context TEXT,
    text TEXT NOT NULL,
    text_normalized TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    segment_identity_hash TEXT NOT NULL,
    price_values NUMERIC[] NOT NULL DEFAULT '{}',
    currency TEXT NOT NULL DEFAULT 'USD',
    service_mentions TEXT[] NOT NULL DEFAULT '{}',
    offer_terms TEXT[] NOT NULL DEFAULT '{}',
    is_price_signal BOOLEAN NOT NULL DEFAULT FALSE,
    is_offer_signal BOOLEAN NOT NULL DEFAULT FALSE,
    content_quality_score NUMERIC,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    missing_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (promo_website_id, segment_index, text_hash)
);

CREATE TABLE IF NOT EXISTS promo_offer_evidence (
    evidence_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id BIGINT NOT NULL REFERENCES promo_offer_master(id) ON DELETE CASCADE,
    segment_id UUID NOT NULL REFERENCES promo_page_segments(segment_id) ON DELETE CASCADE,
    promo_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE SET NULL,
    evidence_role TEXT NOT NULL,
    evidence_text TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    confidence NUMERIC,
    last_verified_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (offer_id, segment_id, evidence_role)
);

CREATE TABLE IF NOT EXISTS promo_offer_change_events (
    change_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promo_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE SET NULL,
    source_url TEXT NOT NULL,
    source_url_normalized TEXT NOT NULL,
    business_id BIGINT REFERENCES master_business_info(business_id) ON DELETE SET NULL,
    crawl_run_id UUID REFERENCES promo_crawl_runs(crawl_run_id) ON DELETE SET NULL,
    monitor_event_id TEXT,
    diff_type TEXT,
    business_change_type TEXT NOT NULL DEFAULT 'unknown',
    affected_segment_ids UUID[] NOT NULL DEFAULT '{}',
    before_text TEXT,
    after_text TEXT,
    before_hash TEXT,
    after_hash TEXT,
    proposed_action TEXT NOT NULL,
    target_offer_id BIGINT REFERENCES promo_offer_master(id) ON DELETE SET NULL,
    proposed_field_updates JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposed_new_offer JSONB NOT NULL DEFAULT '{}'::jsonb,
    confidence NUMERIC,
    confidence_label TEXT,
    reason TEXT,
    validator_status TEXT NOT NULL DEFAULT 'pending',
    validator_errors JSONB NOT NULL DEFAULT '[]'::jsonb,
    applied_at TIMESTAMPTZ,
    applied_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (validator_status IN ('pending', 'applying', 'auto_apply', 'applied', 'needs_review', 'rejected'))
);

CREATE TABLE IF NOT EXISTS promo_offer_match_candidates (
    match_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    change_event_id UUID NOT NULL REFERENCES promo_offer_change_events(change_event_id) ON DELETE CASCADE,
    segment_id UUID REFERENCES promo_page_segments(segment_id) ON DELETE SET NULL,
    candidate_offer_id BIGINT REFERENCES promo_offer_master(id) ON DELETE CASCADE,
    match_score NUMERIC NOT NULL,
    match_method TEXT NOT NULL,
    score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    rank INTEGER,
    is_selected BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS promo_offer_status_history (
    status_history_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id BIGINT REFERENCES promo_offer_master(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    change_event_id UUID REFERENCES promo_offer_change_events(change_event_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_promo_page_segments_business ON promo_page_segments(business_id);
CREATE INDEX IF NOT EXISTS idx_promo_offer_evidence_offer ON promo_offer_evidence(offer_id, status);
CREATE INDEX IF NOT EXISTS idx_promo_offer_change_events_status ON promo_offer_change_events(validator_status, created_at DESC);
