-- M016: extraction quality guardrails (idempotent, live-schema aligned).
-- Preflight: scripts/audit_extraction_quality.py + scripts/apply_extraction_repairs.py

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM promo_offer_master
        WHERE is_active = true
          AND COALESCE(discount_price, 0) <= 0
          AND COALESCE(discount_percent, 0) <= 0
          AND COALESCE(discount_amount, 0) <= 0
    ) THEN
        RAISE EXCEPTION 'M016 blocked: active promo rows missing positive discount field';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_promo_has_discount'
    ) THEN
        ALTER TABLE promo_offer_master
            ADD CONSTRAINT chk_promo_has_discount
            CHECK (
                is_active = false
                OR COALESCE(discount_price, 0) > 0
                OR COALESCE(discount_percent, 0) > 0
                OR COALESCE(discount_amount, 0) > 0
            );
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_promo_offer_active_fingerprint
    ON promo_offer_master (offer_fingerprint)
    WHERE is_active = true AND offer_fingerprint IS NOT NULL;

COMMIT;
