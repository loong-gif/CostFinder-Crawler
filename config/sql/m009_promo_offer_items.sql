BEGIN;

-- M009: normalize offer composition onto promo_offer_items.
-- Master keeps the deal (price + offer_type + price_model).
-- Items keep services (service_id / quantity / unit / area).
-- After backfill, master.service_id is dropped.

DO $$
DECLARE
    required_column TEXT;
BEGIN
    IF to_regclass('public.promo_offer_master') IS NULL THEN
        RAISE EXCEPTION 'M009 requires public.promo_offer_master';
    END IF;
    IF to_regclass('public.clinic_services') IS NULL THEN
        RAISE EXCEPTION 'M009 requires public.clinic_services';
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'promo_offer_master'
          AND column_name = 'id'
    ) THEN
        RAISE EXCEPTION 'M009 requires public.promo_offer_master.id';
    END IF;

    FOREACH required_column IN ARRAY ARRAY[
        'service_id',
        'service_name',
        'unit_type',
        'service_area'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'clinic_services'
              AND column_name = required_column
        ) THEN
            RAISE EXCEPTION
                'M009 requires public.clinic_services.%', required_column;
        END IF;
    END LOOP;
END $$;

ALTER TABLE promo_offer_master
    ADD COLUMN IF NOT EXISTS offer_type TEXT;

ALTER TABLE promo_offer_master
    ADD COLUMN IF NOT EXISTS price_model TEXT;

-- Existing offers become single/total until writers classify them explicitly.
UPDATE promo_offer_master
SET offer_type = 'single'
WHERE offer_type IS NULL;

UPDATE promo_offer_master
SET price_model = 'total'
WHERE price_model IS NULL;

ALTER TABLE promo_offer_master
    ALTER COLUMN offer_type SET DEFAULT 'single',
    ALTER COLUMN offer_type SET NOT NULL;

ALTER TABLE promo_offer_master
    ALTER COLUMN price_model SET DEFAULT 'total',
    ALTER COLUMN price_model SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'promo_offer_master_offer_type_check'
          AND conrelid = 'public.promo_offer_master'::regclass
    ) THEN
        ALTER TABLE promo_offer_master
            ADD CONSTRAINT promo_offer_master_offer_type_check
            CHECK (offer_type IN ('single', 'package'));
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'promo_offer_master_price_model_check'
          AND conrelid = 'public.promo_offer_master'::regclass
    ) THEN
        ALTER TABLE promo_offer_master
            ADD CONSTRAINT promo_offer_master_price_model_check
            CHECK (price_model IN ('total', 'per_unit', 'from'));
    END IF;
END $$;

COMMENT ON COLUMN promo_offer_master.offer_type IS
    'single = one service line; package = bundled deal even when it has one item.';
COMMENT ON COLUMN promo_offer_master.price_model IS
    'total = whole-offer price; per_unit = price per unit_type; from = starting-at price.';

CREATE TABLE IF NOT EXISTS promo_offer_items (
    offer_item_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offer_id BIGINT NOT NULL REFERENCES promo_offer_master(id) ON DELETE CASCADE,
    service_id BIGINT REFERENCES clinic_services(service_id) ON DELETE SET NULL,
    item_name TEXT NOT NULL CHECK (btrim(item_name) <> ''),
    quantity NUMERIC CHECK (quantity IS NULL OR quantity > 0),
    unit_type TEXT,
    service_area TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_promo_offer_items_offer_id
    ON promo_offer_items (offer_id);

CREATE INDEX IF NOT EXISTS idx_promo_offer_items_service_id
    ON promo_offer_items (service_id)
    WHERE service_id IS NOT NULL;

ALTER TABLE promo_offer_items ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE promo_offer_items IS
    'Services contained by an offer. Single offers have one row; packages have one or more.';
COMMENT ON COLUMN promo_offer_items.quantity IS
    'Positive stated quantity only. Per-unit or unspecified amounts stay NULL.';

-- Move service linkage off master onto items. quantity stays NULL unless known.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'promo_offer_master'
          AND column_name = 'service_id'
    ) THEN
        INSERT INTO promo_offer_items (
            offer_id,
            service_id,
            item_name,
            quantity,
            unit_type,
            service_area
        )
        SELECT
            o.id,
            o.service_id,
            COALESCE(NULLIF(btrim(cs.service_name), ''), 'Offer item'),
            NULL,
            NULLIF(btrim(cs.unit_type), ''),
            NULLIF(btrim(cs.service_area), '')
        FROM promo_offer_master AS o
        LEFT JOIN clinic_services AS cs ON cs.service_id = o.service_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM promo_offer_items AS existing
            WHERE existing.offer_id = o.id
        );
    ELSE
        INSERT INTO promo_offer_items (
            offer_id,
            service_id,
            item_name,
            quantity,
            unit_type,
            service_area
        )
        SELECT
            o.id,
            NULL,
            'Offer item',
            NULL,
            NULL,
            NULL
        FROM promo_offer_master AS o
        WHERE NOT EXISTS (
            SELECT 1
            FROM promo_offer_items AS existing
            WHERE existing.offer_id = o.id
        );
    END IF;
END $$;

ALTER TABLE promo_offer_master
    DROP CONSTRAINT IF EXISTS fk_offer_service;

ALTER TABLE promo_offer_master
    DROP COLUMN IF EXISTS service_id;

DO $$
DECLARE
    missing_offer_count BIGINT;
BEGIN
    SELECT COUNT(*)
    INTO missing_offer_count
    FROM promo_offer_master AS o
    WHERE NOT EXISTS (
        SELECT 1
        FROM promo_offer_items AS i
        WHERE i.offer_id = o.id
    );

    IF missing_offer_count > 0 THEN
        RAISE EXCEPTION
            'M009 validation failed: % offer rows without promo_offer_items',
            missing_offer_count;
    END IF;
END $$;

COMMIT;
