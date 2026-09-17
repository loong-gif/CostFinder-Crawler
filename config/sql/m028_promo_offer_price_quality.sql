BEGIN;

-- ``discount_amount`` and ``discount_percent`` are derived values whenever a
-- complete price pair is stored.  Do not retain stale values supplied by an
-- earlier extraction run.
CREATE OR REPLACE FUNCTION public.fn_auto_fix_offer_prices()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    IF NEW.regular_price IS NOT NULL
       AND NEW.discount_price IS NOT NULL
       AND NEW.regular_price > 0
       AND NEW.discount_price >= 0
       AND NEW.discount_price < NEW.regular_price THEN
        NEW.discount_amount := round(NEW.regular_price - NEW.discount_price, 2);
        NEW.discount_percent := round(
            (NEW.regular_price - NEW.discount_price) * 100 / NEW.regular_price,
            2
        );
    END IF;
    RETURN NEW;
END;
$function$;

-- Unit price has different semantics for each price model.  An unlinked item,
-- a mixed-service package, a starting-at price, or a non-terminating total has
-- no safely allocable per-item price.
CREATE OR REPLACE FUNCTION public.normalize_promo_offer_item_unit_price()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
DECLARE
    v_discount_price numeric;
    v_price_model text;
    v_other_quantity numeric;
    v_has_different_item boolean;
    v_total_quantity numeric;
    v_derived_unit_price numeric;
BEGIN
    SELECT discount_price, price_model
      INTO v_discount_price, v_price_model
      FROM public.promo_offer_master
     WHERE id = NEW.offer_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No promo_offer_master found for offer_id=%', NEW.offer_id;
    END IF;

    SELECT
        COALESCE(sum(existing.quantity), 0),
        COALESCE(bool_or(
            existing.service_id IS NULL
            OR existing.service_id IS DISTINCT FROM NEW.service_id
        ), false)
      INTO v_other_quantity, v_has_different_item
      FROM public.promo_offer_items AS existing
     WHERE existing.offer_id = NEW.offer_id
       AND (TG_OP = 'INSERT' OR existing.offer_item_id <> NEW.offer_item_id);

    IF NEW.service_id IS NULL
       OR v_has_different_item
       OR v_discount_price IS NULL
       OR v_price_model = 'from' THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    IF v_price_model = 'per_unit' THEN
        v_derived_unit_price := round(v_discount_price, 4);
        IF v_derived_unit_price <> v_discount_price THEN
            NEW.unit_price := NULL;
        ELSE
            NEW.unit_price := v_derived_unit_price;
        END IF;
        RETURN NEW;
    END IF;

    IF v_price_model <> 'total' THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    v_total_quantity := COALESCE(NEW.quantity, 0) + v_other_quantity;
    IF v_total_quantity <= 0 THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    v_derived_unit_price := round(v_discount_price / v_total_quantity, 4);
    IF v_derived_unit_price * v_total_quantity <> v_discount_price THEN
        NEW.unit_price := NULL;
    ELSE
        NEW.unit_price := v_derived_unit_price;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.sync_promo_offer_item_unit_prices()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    -- Updating the column causes the BEFORE normalizer above to calculate from
    -- the already-updated master row.  The trigger is intentionally a no-op
    -- for master changes that do not affect unit-price semantics.
    UPDATE public.promo_offer_items
       SET unit_price = unit_price
     WHERE offer_id = NEW.id;
    RETURN NULL;
END;
$function$;

DROP TRIGGER IF EXISTS trg_sync_promo_offer_item_unit_prices ON public.promo_offer_master;
CREATE TRIGGER trg_sync_promo_offer_item_unit_prices
AFTER UPDATE OF discount_price, price_model ON public.promo_offer_master
FOR EACH ROW
WHEN (
    OLD.discount_price IS DISTINCT FROM NEW.discount_price
    OR OLD.price_model IS DISTINCT FROM NEW.price_model
)
EXECUTE FUNCTION public.sync_promo_offer_item_unit_prices();

-- Backfill the derived fields before enforcing their database invariants.
UPDATE public.promo_offer_master
   SET discount_amount = round(regular_price - discount_price, 2),
       discount_percent = round(
           (regular_price - discount_price) * 100 / regular_price,
           2
       )
 WHERE regular_price IS NOT NULL
   AND discount_price IS NOT NULL
   AND regular_price > discount_price;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_promo_offer_derived_discount_amount'
          AND conrelid = 'public.promo_offer_master'::regclass
    ) THEN
        ALTER TABLE public.promo_offer_master
            ADD CONSTRAINT chk_promo_offer_derived_discount_amount
            CHECK (
                regular_price IS NULL
                OR discount_price IS NULL
                OR discount_price >= regular_price
                OR discount_amount IS NULL
                OR abs(discount_amount - round(regular_price - discount_price, 2)) <= 0.01
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_promo_offer_derived_discount_percent'
          AND conrelid = 'public.promo_offer_master'::regclass
    ) THEN
        ALTER TABLE public.promo_offer_master
            ADD CONSTRAINT chk_promo_offer_derived_discount_percent
            CHECK (
                regular_price IS NULL
                OR discount_price IS NULL
                OR discount_price >= regular_price
                OR discount_percent IS NULL
                OR abs(
                    discount_percent
                    - round((regular_price - discount_price) * 100 / regular_price, 2)
                ) <= 0.01
            );
    END IF;
END $$;

-- Normalize all historical item values under the new semantics.
UPDATE public.promo_offer_items
   SET unit_price = unit_price;

-- RLS has no policies on this internal work queue; browser roles are also
-- explicitly revoked. Only controlled server-side credentials can access it.
CREATE TABLE IF NOT EXISTS public.promo_offer_quality_reviews (
    review_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offer_id BIGINT NOT NULL REFERENCES public.promo_offer_master(id) ON DELETE CASCADE,
    rule_name TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposed_patch JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'corrected', 'split', 'inactive', 'retained')),
    resolution_note TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (offer_id, rule_name)
);

CREATE INDEX IF NOT EXISTS idx_promo_offer_quality_reviews_pending
    ON public.promo_offer_quality_reviews (created_at DESC)
    WHERE status = 'pending';

ALTER TABLE public.promo_offer_quality_reviews ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.promo_offer_quality_reviews FROM anon, authenticated;

DROP TRIGGER IF EXISTS trg_promo_offer_quality_reviews_updated_at ON public.promo_offer_quality_reviews;
CREATE TRIGGER trg_promo_offer_quality_reviews_updated_at
BEFORE UPDATE ON public.promo_offer_quality_reviews
FOR EACH ROW
EXECUTE FUNCTION public.update_updated_at_column();

COMMIT;
