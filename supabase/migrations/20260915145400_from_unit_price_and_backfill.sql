-- Allow price_model=from to keep unit_price.
-- If quantity is null: discount_price = unit_price (same for total/from).
-- Backfill active non-package missing unit_price, and unambiguous service regular_price.

CREATE OR REPLACE FUNCTION public.normalize_promo_offer_item_unit_price()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'public'
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

    IF NEW.service_id IS NULL OR v_discount_price IS NULL THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    IF v_price_model = 'per_unit' THEN
        v_derived_unit_price := round(v_discount_price, 4);
        NEW.unit_price := CASE
            WHEN v_derived_unit_price = v_discount_price THEN v_derived_unit_price
            ELSE NULL
        END;
        RETURN NEW;
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

    IF v_has_different_item OR v_price_model NOT IN ('total', 'from') THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    v_total_quantity := COALESCE(NEW.quantity, 0) + v_other_quantity;
    IF v_total_quantity <= 0 THEN
        NEW.unit_price := round(v_discount_price, 4);
        RETURN NEW;
    END IF;

    v_derived_unit_price := round(v_discount_price / v_total_quantity, 4);
    NEW.unit_price := CASE
        WHEN abs(v_derived_unit_price * v_total_quantity - v_discount_price) <= 0.05
            THEN v_derived_unit_price
        ELSE NULL
    END;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.sync_promo_offer_item_unit_prices()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
    v_is_multi_item_package boolean;
    v_total_quantity numeric;
    v_derived_unit_price numeric;
    v_rounding_delta numeric;
BEGIN
    SELECT
        count(*) > 1 AND (
            count(*) FILTER (WHERE service_id IS NULL) > 0
            OR count(DISTINCT service_id) > 1
        ),
        coalesce(sum(quantity), 0)
      INTO v_is_multi_item_package, v_total_quantity
      FROM public.promo_offer_items
     WHERE offer_id = NEW.id;

    IF v_is_multi_item_package OR NEW.discount_price IS NULL THEN
        UPDATE public.promo_offer_items
           SET unit_price = NULL
         WHERE offer_id = NEW.id
           AND unit_price IS NOT NULL;
        RETURN NULL;
    END IF;

    IF NEW.price_model = 'per_unit' THEN
        UPDATE public.promo_offer_items
           SET unit_price = round(NEW.discount_price, 4)
         WHERE offer_id = NEW.id
           AND service_id IS NOT NULL
           AND unit_price IS DISTINCT FROM round(NEW.discount_price, 4);
        RETURN NULL;
    END IF;

    IF NEW.price_model NOT IN ('total', 'from') THEN
        RETURN NULL;
    END IF;

    IF v_total_quantity <= 0 THEN
        UPDATE public.promo_offer_items
           SET unit_price = round(NEW.discount_price, 4)
         WHERE offer_id = NEW.id
           AND service_id IS NOT NULL
           AND unit_price IS DISTINCT FROM round(NEW.discount_price, 4);
        RETURN NULL;
    END IF;

    v_derived_unit_price := round(NEW.discount_price / v_total_quantity, 4);
    v_rounding_delta := abs(v_derived_unit_price * v_total_quantity - NEW.discount_price);

    IF v_rounding_delta > 0.05 THEN
        RETURN NULL;
    END IF;

    IF OLD.price_model IS DISTINCT FROM NEW.price_model THEN
        UPDATE public.promo_offer_items
           SET unit_price = v_derived_unit_price
         WHERE offer_id = NEW.id
           AND service_id IS NOT NULL;
        RETURN NULL;
    END IF;

    UPDATE public.promo_offer_items
       SET unit_price = v_derived_unit_price
     WHERE offer_id = NEW.id
       AND service_id IS NOT NULL
       AND (
            unit_price IS NULL
            OR OLD.discount_price IS NULL
            OR abs(unit_price * v_total_quantity - OLD.discount_price) <= 0.05
            OR (v_total_quantity > 1 AND unit_price >= coalesce(OLD.discount_price, 0))
       );

    RETURN NULL;
END;
$function$;

-- Touch fillable rows so normalize derives unit_price.
UPDATE public.promo_offer_items i
SET unit_price = i.unit_price
FROM public.promo_offer_master m
WHERE i.offer_id = m.id
  AND m.is_active
  AND m.discount_price IS NOT NULL
  AND i.unit_price IS NULL
  AND i.service_id IS NOT NULL
  AND coalesce(m.is_package, false) = false;

-- Fill catalog regular_price only when every active offer agrees on one value.
WITH proposed AS (
  SELECT
    i.service_id,
    CASE
      WHEN i.quantity IS NULL OR i.quantity = 1 THEN m.regular_price
      ELSE round(m.regular_price / i.quantity, 2)
    END AS proposed
  FROM public.promo_offer_items i
  JOIN public.promo_offer_master m ON m.id = i.offer_id
  WHERE m.is_active
    AND m.regular_price IS NOT NULL
    AND i.service_id IS NOT NULL
),
ok AS (
  SELECT service_id, min(proposed) AS proposed
  FROM proposed
  GROUP BY service_id
  HAVING count(DISTINCT proposed) = 1
)
UPDATE public.clinic_services s
SET regular_price = ok.proposed
FROM ok
WHERE s.service_id = ok.service_id
  AND s.regular_price IS NULL;
