-- M024: Agent database defense patterns (zero ALTER TABLE ADD COLUMN).
-- Patterns: role timeouts, idempotency on monitor_event_id, query monitoring,
-- semantic agent-facing views with audit fields from change_events.

BEGIN;

-- Pattern 1: bind timeouts on the API-facing role
ALTER ROLE authenticated SET statement_timeout = '30s';
ALTER ROLE authenticated SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE authenticated SET application_name = 'cf-api';

-- Pattern 3: idempotency via existing monitor_event_id column
DO $$
BEGIN
    IF to_regclass('public.promo_offer_change_events') IS NULL THEN
        RAISE EXCEPTION 'M024 requires public.promo_offer_change_events';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_promo_offer_change_events_monitor
  ON promo_offer_change_events (monitor_event_id)
  WHERE monitor_event_id IS NOT NULL;

-- Pattern 5: query monitoring extension
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Pattern 6: agent-facing views (audit fields computed, not stored)
CREATE OR REPLACE VIEW active_promo_offers AS
  SELECT m.id, m.business_id, m.promotion_id, m.offer_raw_text,
         m.service_category, m.regular_price, m.discount_price,
         m.discount_amount, m.discount_percent, m.is_membership_required,
         m.is_new_customer_required, m.is_active, m.offer_type,
         m.price_model, m.offer_fingerprint, m.status,
         m.created_at, m.updated_at, m.last_verified_at,
         (SELECT e.applied_by FROM promo_offer_change_events e
          WHERE e.target_offer_id = m.id
          ORDER BY e.created_at DESC LIMIT 1) AS last_changed_by,
         (SELECT e.reason FROM promo_offer_change_events e
          WHERE e.target_offer_id = m.id
          ORDER BY e.created_at DESC LIMIT 1) AS last_change_reason
  FROM promo_offer_master m
  WHERE m.deleted_at IS NULL;

CREATE OR REPLACE VIEW active_businesses AS
  SELECT b.business_id, b.business_name, b.address, b.city, b.state,
         b.zip_code, b.phone, b.website, b.process_flag,
         b.created_at, b.updated_at
  FROM master_business_info b
  WHERE b.deleted_at IS NULL;

COMMENT ON VIEW active_promo_offers IS
  'Agent view: non-deleted offers. last_changed_by/last_change_reason come from change_events.';
COMMENT ON COLUMN active_promo_offers.is_active IS
  'Whether the offer is currently active. false = ended but not soft-deleted.';
COMMENT ON COLUMN active_promo_offers.offer_type IS
  'Offer type: single, bundle, membership.';
COMMENT ON COLUMN active_promo_offers.price_model IS
  'Price model: total, per_unit, from.';
COMMENT ON COLUMN active_businesses.process_flag IS
  'Processing flag. NULL=unprocessed, verified=verified, ineligible=ineligible.';

COMMIT;
