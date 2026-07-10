-- M003: promo_offer_master offer_fingerprint support
--
-- Part A (safe anytime): ensure column exists, drop legacy lifecycle_status index.
-- Part B (run AFTER scripts/dedupe_promo_offer_master.py): active fingerprint unique index.

BEGIN;

ALTER TABLE promo_offer_master
  ADD COLUMN IF NOT EXISTS offer_fingerprint TEXT;

DROP INDEX IF EXISTS idx_promo_offer_master_active_fingerprint;

COMMIT;

-- ---------------------------------------------------------------------------
-- Part B: execute only after dedupe_promo_offer_master.py reports zero dup groups
--   python scripts/apply_sql_migration.py config/sql/m003b_promo_offer_active_fp_index.sql
-- ---------------------------------------------------------------------------
