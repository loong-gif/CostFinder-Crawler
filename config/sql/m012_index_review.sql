-- M012: index review follow-up (2026-07-17).
-- Adds missing FK/consumer indexes; drops unused redundant composite on clinic_promotions.
-- Live schema uses is_active (not legacy status).

BEGIN;

-- FK: promo_offer_master.promotion_id → clinic_promotions (advisor 0001_unindexed_foreign_keys)
CREATE INDEX IF NOT EXISTS idx_promo_offer_master_promotion_id
    ON promo_offer_master (promotion_id)
    WHERE promotion_id IS NOT NULL;

-- Consumer listings: is_active + created_at (marketplace freshness, offer feeds)
CREATE INDEX IF NOT EXISTS idx_promo_offer_master_active_created
    ON promo_offer_master (created_at DESC)
    WHERE is_active = true;

-- Featured offers: discount_percent sort with price guards (offers.ts getFeaturedOffers)
CREATE INDEX IF NOT EXISTS idx_promo_offer_master_active_featured
    ON promo_offer_master (discount_percent DESC NULLS LAST, created_at DESC)
    WHERE is_active = true
      AND discount_price > 0
      AND regular_price > 0;

-- Redundant with idx_promotions_business_id prefix; 0 scans since stats reset
DROP INDEX IF EXISTS idx_promotions_business_active_dates;

COMMIT;
