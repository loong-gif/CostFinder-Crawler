-- M003b: unique active offer fingerprint (post-dedupe only)

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS idx_promo_offer_master_active_fp
  ON promo_offer_master (offer_fingerprint)
  WHERE offer_fingerprint IS NOT NULL AND status = 'active';

COMMIT;
