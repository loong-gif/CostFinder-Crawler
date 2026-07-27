BEGIN;

-- The prior trigger rewrote every explicit NULL as discount_price / quantity.
-- That loses the fact that a multi-service package has no allocable unit price.
DROP TRIGGER IF EXISTS trg_set_promo_offer_items_unit_price ON public.promo_offer_items;

-- Whole-offer and item-unit amounts must use decimal, not floating-point,
-- storage. Four places covers the current price ingestion contract.
ALTER TABLE public.promo_offer_items
    ALTER COLUMN unit_price TYPE numeric(12,4)
    USING round(unit_price::numeric, 4);

CREATE OR REPLACE FUNCTION public.normalize_promo_offer_item_unit_price()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    v_discount_price numeric;
    v_other_quantity integer;
    v_has_different_item boolean;
    v_total_quantity integer;
    v_derived_unit_price numeric;
BEGIN
    SELECT discount_price
    INTO v_discount_price
    FROM public.promo_offer_master
    WHERE id = NEW.offer_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No promo_offer_master found for offer_id=%', NEW.offer_id;
    END IF;

    SELECT
        COALESCE(sum(existing.quantity), 0),
        COALESCE(bool_or(
            NEW.service_id IS NULL
            OR existing.service_id IS NULL
            OR existing.service_id IS DISTINCT FROM NEW.service_id
        ), false)
    INTO v_other_quantity, v_has_different_item
    FROM public.promo_offer_items AS existing
    WHERE existing.offer_id = NEW.offer_id
      AND (TG_OP = 'INSERT' OR existing.offer_item_id <> NEW.offer_item_id);

    IF v_has_different_item THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    v_total_quantity := COALESCE(NEW.quantity, 0) + v_other_quantity;
    IF v_discount_price IS NULL OR v_total_quantity <= 0 THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    v_derived_unit_price := round(v_discount_price / v_total_quantity, 4);
    -- Do not invent a rounded unit amount that fails the stated invariant.
    IF v_derived_unit_price * v_total_quantity <> v_discount_price THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    NEW.unit_price := v_derived_unit_price;
    RETURN NEW;
END;
$function$;

CREATE TRIGGER trg_normalize_promo_offer_item_unit_price
BEFORE INSERT OR UPDATE OF offer_id, service_id, quantity, unit_price
ON public.promo_offer_items
FOR EACH ROW
EXECUTE FUNCTION public.normalize_promo_offer_item_unit_price();

CREATE OR REPLACE FUNCTION public.clear_package_item_unit_prices_for_offer(p_offer_id bigint)
RETURNS void
LANGUAGE plpgsql
AS $function$
DECLARE
    v_is_multi_item_package boolean;
BEGIN
    SELECT
        count(*) > 1
        AND (
            count(*) FILTER (WHERE service_id IS NULL) > 0
            OR count(DISTINCT service_id) > 1
        )
    INTO v_is_multi_item_package
    FROM public.promo_offer_items
    WHERE offer_id = p_offer_id;

    IF v_is_multi_item_package THEN
        UPDATE public.promo_offer_items
        SET unit_price = NULL
        WHERE offer_id = p_offer_id
          AND unit_price IS NOT NULL;
    END IF;
END;
$function$;

CREATE OR REPLACE FUNCTION public.clear_package_item_unit_prices()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    -- Lock the affected master row(s) to serialize package composition changes.
    PERFORM 1 FROM public.promo_offer_master WHERE id = NEW.offer_id FOR UPDATE;
    PERFORM public.clear_package_item_unit_prices_for_offer(NEW.offer_id);
    IF TG_OP = 'UPDATE' AND OLD.offer_id IS DISTINCT FROM NEW.offer_id THEN
        PERFORM 1 FROM public.promo_offer_master WHERE id = OLD.offer_id FOR UPDATE;
        PERFORM public.clear_package_item_unit_prices_for_offer(OLD.offer_id);
    END IF;
    RETURN NULL;
END;
$function$;

CREATE TRIGGER trg_clear_package_item_unit_prices
AFTER INSERT OR UPDATE OF offer_id, service_id ON public.promo_offer_items
FOR EACH ROW
EXECUTE FUNCTION public.clear_package_item_unit_prices();

-- Correct all existing different-item packages now. Multiple rows for the same
-- linked service remain one logical item and receive a single derived price.
WITH package_offers AS (
    SELECT offer_id
    FROM public.promo_offer_items
    GROUP BY offer_id
    HAVING count(*) > 1
       AND (
            count(*) FILTER (WHERE service_id IS NULL) > 0
            OR count(DISTINCT service_id) > 1
       )
)
UPDATE public.promo_offer_items AS item
SET unit_price = NULL
FROM package_offers
WHERE item.offer_id = package_offers.offer_id;

-- Normalize all remaining single-logical-service rows only when the four-place
-- unit price can reproduce the whole-offer discount total exactly.
WITH single_logical_offers AS (
    SELECT
        offer_id,
        sum(quantity)::integer AS total_quantity
    FROM public.promo_offer_items
    GROUP BY offer_id
    HAVING count(*) = 1
       OR (
            count(*) > 1
            AND count(*) FILTER (WHERE service_id IS NULL) = 0
            AND count(DISTINCT service_id) = 1
       )
), exact_unit_prices AS (
    SELECT
        item.offer_id,
        round(master.discount_price / logical_offer.total_quantity, 4) AS unit_price
    FROM single_logical_offers AS logical_offer
    JOIN public.promo_offer_master AS master ON master.id = logical_offer.offer_id
    JOIN public.promo_offer_items AS item ON item.offer_id = logical_offer.offer_id
    WHERE master.discount_price IS NOT NULL
      AND logical_offer.total_quantity > 0
      AND round(master.discount_price / logical_offer.total_quantity, 4)
          * logical_offer.total_quantity = master.discount_price
)
UPDATE public.promo_offer_items AS item
SET unit_price = exact_unit_prices.unit_price
FROM exact_unit_prices
WHERE item.offer_id = exact_unit_prices.offer_id;

COMMIT;
