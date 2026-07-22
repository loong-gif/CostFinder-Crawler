-- M010: promo_offer_master QA constraints (run after data cleanup).
-- Prerequisite: qa_repair_promo_offer_master has cleared blocking nulls on active rows.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM promo_offer_master
        WHERE status = 'active' AND business_id IS NULL
    ) THEN
        RAISE EXCEPTION 'M010 blocked: active offers still missing business_id';
    END IF;
    IF EXISTS (
        SELECT 1 FROM promo_offer_master
        WHERE status = 'active'
          AND (service_name IS NULL OR btrim(service_name) = '')
    ) THEN
        RAISE EXCEPTION 'M010 blocked: active offers still missing service_name';
    END IF;
    IF EXISTS (
        SELECT 1 FROM promo_offer_master
        WHERE status = 'active'
          AND (source_url IS NULL OR btrim(source_url) = '')
    ) THEN
        RAISE EXCEPTION 'M010 blocked: active offers still missing source_url';
    END IF;
END $$;

ALTER TABLE promo_offer_master
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_promo_offer_master_public_freshness
    ON promo_offer_master (last_verified_at DESC)
    WHERE status = 'active' AND last_verified_at IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'promo_offer_master_status_check'
    ) THEN
        ALTER TABLE promo_offer_master
            ADD CONSTRAINT promo_offer_master_status_check
            CHECK (status IN ('active', 'ended'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'promo_offer_master_prices_nonnegative'
    ) THEN
        ALTER TABLE promo_offer_master
            ADD CONSTRAINT promo_offer_master_prices_nonnegative
            CHECK (
                (regular_price IS NULL OR regular_price >= 0)
                AND (discount_price IS NULL OR discount_price >= 0)
                AND (discount_amount IS NULL OR discount_amount >= 0)
                AND (discount_percent IS NULL OR (discount_percent >= 0 AND discount_percent <= 100))
            );
    END IF;
END $$;

COMMIT;
