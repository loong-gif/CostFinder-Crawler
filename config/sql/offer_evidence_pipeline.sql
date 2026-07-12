-- LEGACY DESIGN: this file was authored before the production schema audit.
-- Do not run it directly against production. Use m006_evidence_pipeline_bootstrap.sql
-- after running scripts/audit_schema_preflight.py and reviewing its output.
-- It still documents the original full evidence design for reference.

-- Offer evidence pipeline schema for CostFinder.
--
-- Purpose:
--   Preserve a traceable chain from crawled page text -> segment evidence ->
--   candidate offer match -> validated change event -> promo_offer_master.
--
-- This migration is additive and safe to run before application code uses the
-- new tables. Existing promo_website_staging / promo_offer_master rows remain
-- untouched except for nullable identity/audit columns added to master.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- Crawl run snapshots
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS promo_crawl_runs (
    crawl_run_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promo_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE SET NULL,
    source_url TEXT NOT NULL,
    source_url_normalized TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'success',
    content_hash TEXT,
    business_hash TEXT,
    segment_count INTEGER NOT NULL DEFAULT 0,
    offer_signal_count INTEGER NOT NULL DEFAULT 0,
    price_signal_count INTEGER NOT NULL DEFAULT 0,
    content_quality_score NUMERIC,
    content_quality_state TEXT NOT NULL DEFAULT 'ok',
    error TEXT,
    raw_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_promo_crawl_runs_website
    ON promo_crawl_runs (promo_website_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_promo_crawl_runs_url
    ON promo_crawl_runs (source_url_normalized, started_at DESC);

COMMENT ON TABLE promo_crawl_runs IS
    'One crawl/scrape attempt per promo_website_staging URL, including quality metrics used to gate offer updates.';

-- ---------------------------------------------------------------------------
-- Segment evidence extracted from page_content
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS promo_page_segments (
    segment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promo_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE CASCADE,
    crawl_run_id UUID REFERENCES promo_crawl_runs(crawl_run_id) ON DELETE SET NULL,
    business_id BIGINT,
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
    brand_mentions TEXT[] NOT NULL DEFAULT '{}',
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_promo_page_segments_run_index_hash
    ON promo_page_segments (promo_website_id, segment_index, text_hash);

CREATE INDEX IF NOT EXISTS idx_promo_page_segments_url
    ON promo_page_segments (source_url_normalized, segment_index);

CREATE INDEX IF NOT EXISTS idx_promo_page_segments_business
    ON promo_page_segments (business_id);

CREATE INDEX IF NOT EXISTS idx_promo_page_segments_identity
    ON promo_page_segments (segment_identity_hash);

CREATE INDEX IF NOT EXISTS idx_promo_page_segments_semantic_hash
    ON promo_page_segments (semantic_hash);

CREATE INDEX IF NOT EXISTS idx_promo_page_segments_signals
    ON promo_page_segments (is_offer_signal, is_price_signal);

CREATE INDEX IF NOT EXISTS idx_promo_page_segments_service_mentions
    ON promo_page_segments USING GIN (service_mentions);

CREATE INDEX IF NOT EXISTS idx_promo_page_segments_price_values
    ON promo_page_segments USING GIN (price_values);

COMMENT ON TABLE promo_page_segments IS
    'Stable evidence units parsed from promo_website_staging.page_content for diffing and offer attribution.';

-- ---------------------------------------------------------------------------
-- Evidence links from structured offers back to source segments
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS promo_offer_evidence (
    evidence_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id BIGINT REFERENCES promo_offer_master(id) ON DELETE CASCADE,
    segment_id UUID REFERENCES promo_page_segments(segment_id) ON DELETE CASCADE,
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

CREATE INDEX IF NOT EXISTS idx_promo_offer_evidence_offer
    ON promo_offer_evidence (offer_id, status);

CREATE INDEX IF NOT EXISTS idx_promo_offer_evidence_segment
    ON promo_offer_evidence (segment_id, status);

COMMENT ON TABLE promo_offer_evidence IS
    'Many-to-many evidence map connecting promo_offer_master rows to source page segments and field roles.';

-- ---------------------------------------------------------------------------
-- Master offer identity / lifecycle columns
-- ---------------------------------------------------------------------------

ALTER TABLE promo_offer_master
    ADD COLUMN IF NOT EXISTS source_url_normalized TEXT,
    ADD COLUMN IF NOT EXISTS source_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS raw_service_name TEXT,
    ADD COLUMN IF NOT EXISTS display_service_name TEXT,
    ADD COLUMN IF NOT EXISTS canonical_service_name TEXT,
    ADD COLUMN IF NOT EXISTS offer_type TEXT,
    ADD COLUMN IF NOT EXISTS price_model TEXT,
    ADD COLUMN IF NOT EXISTS quantity NUMERIC,
    ADD COLUMN IF NOT EXISTS vendor_program TEXT,
    ADD COLUMN IF NOT EXISTS offer_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS price_signature TEXT,
    ADD COLUMN IF NOT EXISTS identity_confidence NUMERIC,
    ADD COLUMN IF NOT EXISTS evidence_hash TEXT,
    ADD COLUMN IF NOT EXISTS last_evidence_seen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_crawl_run_id UUID REFERENCES promo_crawl_runs(crawl_run_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS lifecycle_status TEXT NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS missing_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS ended_reason TEXT,
    ADD COLUMN IF NOT EXISTS superseded_by_offer_id BIGINT REFERENCES promo_offer_master(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_promo_offer_master_source_url_normalized
    ON promo_offer_master (source_url_normalized);

CREATE INDEX IF NOT EXISTS idx_promo_offer_master_business_canonical
    ON promo_offer_master (business_id, canonical_service_name);

CREATE INDEX IF NOT EXISTS idx_promo_offer_master_lifecycle
    ON promo_offer_master (business_id, lifecycle_status);

CREATE INDEX IF NOT EXISTS idx_promo_offer_master_offer_type
    ON promo_offer_master (offer_type);

CREATE INDEX IF NOT EXISTS idx_promo_offer_master_price_model
    ON promo_offer_master (price_model);

CREATE UNIQUE INDEX IF NOT EXISTS idx_promo_offer_master_active_fingerprint
    ON promo_offer_master (offer_fingerprint)
    WHERE offer_fingerprint IS NOT NULL
      AND lifecycle_status IN ('active', 'missing_once', 'stale_candidate', 'needs_review');

-- ---------------------------------------------------------------------------
-- Change events and candidate matches
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS promo_offer_change_events (
    change_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    promo_website_id BIGINT REFERENCES promo_website_staging(promo_website_id) ON DELETE SET NULL,
    source_url TEXT NOT NULL,
    source_url_normalized TEXT NOT NULL,
    business_id BIGINT,
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_promo_offer_change_events_url
    ON promo_offer_change_events (source_url_normalized, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_promo_offer_change_events_business
    ON promo_offer_change_events (business_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_promo_offer_change_events_target
    ON promo_offer_change_events (target_offer_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_promo_offer_change_events_status
    ON promo_offer_change_events (validator_status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_promo_offer_change_events_action
    ON promo_offer_change_events (proposed_action, created_at DESC);

COMMENT ON TABLE promo_offer_change_events IS
    'Audit table for LLM/rule proposed offer changes before validated application to promo_offer_master.';

CREATE TABLE IF NOT EXISTS promo_offer_match_candidates (
    match_candidate_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    change_event_id UUID REFERENCES promo_offer_change_events(change_event_id) ON DELETE CASCADE,
    segment_id UUID REFERENCES promo_page_segments(segment_id) ON DELETE SET NULL,
    candidate_offer_id BIGINT REFERENCES promo_offer_master(id) ON DELETE CASCADE,
    match_score NUMERIC NOT NULL,
    match_method TEXT NOT NULL,
    score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    rank INTEGER,
    is_selected BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_promo_offer_match_candidates_event
    ON promo_offer_match_candidates (change_event_id, rank);

CREATE INDEX IF NOT EXISTS idx_promo_offer_match_candidates_segment
    ON promo_offer_match_candidates (segment_id);

CREATE INDEX IF NOT EXISTS idx_promo_offer_match_candidates_offer
    ON promo_offer_match_candidates (candidate_offer_id);

CREATE INDEX IF NOT EXISTS idx_promo_offer_match_candidates_score
    ON promo_offer_match_candidates (match_score DESC);

COMMENT ON TABLE promo_offer_match_candidates IS
    'Candidate offer set generated before LLM adjudication, preventing unconstrained master offer selection.';

CREATE TABLE IF NOT EXISTS promo_offer_status_history (
    status_history_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id BIGINT REFERENCES promo_offer_master(id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    change_event_id UUID REFERENCES promo_offer_change_events(change_event_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_promo_offer_status_history_offer
    ON promo_offer_status_history (offer_id, created_at DESC);
