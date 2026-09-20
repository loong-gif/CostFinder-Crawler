BEGIN;

-- An active offer cannot be publicly visible through an inactive parent
-- promotion.  The source record for 8746 is retained, but the offer follows
-- its inactive parent until the promotion is revalidated.
UPDATE public.promo_offer_master AS offer
   SET is_active = false,
       updated_at = now()
 WHERE offer.is_active
   AND offer.promotion_id IS NOT NULL
   AND EXISTS (
       SELECT 1
       FROM public.clinic_promotions AS promotion
       WHERE promotion.promotion_id = offer.promotion_id
         AND NOT COALESCE(promotion.is_active, false)
   );

-- A NULL service_id is not a valid saleable item.  Create only atomic service
-- identities that are explicitly named by the source offer.  Generic scopes
-- ("all services", "injectables", "filler", etc.) are not silently mapped to
-- an unrelated catalogue row.
WITH candidates AS (
    SELECT DISTINCT
           offer.business_id,
           btrim(item.item_name) AS service_name,
           promotion.source_url
    FROM public.promo_offer_items AS item
    JOIN public.promo_offer_master AS offer ON offer.id = item.offer_id
    LEFT JOIN public.clinic_promotions AS promotion ON promotion.promotion_id = offer.promotion_id
    WHERE offer.is_active
      AND item.service_id IS NULL
      AND NULLIF(btrim(item.item_name), '') IS NOT NULL
      AND lower(btrim(item.item_name)) NOT IN (
          'all services', 'additional services and products', 'injectables',
          'filler', 'dermal filler', 'tox treatments', 'tox (md)', 'tox (rn)',
          'skin treatments', 'neurotoxin', 'wrinkle relaxer treatment'
      )
      AND lower(btrim(item.item_name)) NOT LIKE '% or %'
      AND lower(btrim(item.item_name)) NOT LIKE '% and %'
), inserted AS (
    INSERT INTO public.clinic_services (business_id, service_name, source_url)
    SELECT candidate.business_id, candidate.service_name, candidate.source_url
    FROM candidates AS candidate
    WHERE NOT EXISTS (
        SELECT 1
        FROM public.clinic_services AS existing
        WHERE existing.business_id = candidate.business_id
          AND lower(btrim(existing.service_name)) = lower(candidate.service_name)
    )
    RETURNING service_id, business_id, service_name
)
UPDATE public.promo_offer_items AS item
   SET service_id = service.service_id,
       item_name = service.service_name,
       updated_at = now()
  FROM public.promo_offer_master AS offer
  JOIN public.clinic_services AS service
    ON service.business_id = offer.business_id
 WHERE item.offer_id = offer.id
   AND item.service_id IS NULL
   AND lower(btrim(service.service_name)) = lower(btrim(item.item_name));

-- Remaining rows are broad or otherwise non-deterministic scopes.  They must
-- not remain active with a NULL placeholder; quarantine the offer instead of
-- inventing a service relationship.
UPDATE public.promo_offer_master AS offer
   SET is_active = false,
       updated_at = now()
 WHERE offer.is_active
   AND EXISTS (
       SELECT 1
       FROM public.promo_offer_items AS item
       WHERE item.offer_id = offer.id
         AND item.service_id IS NULL
   );

-- A per-unit offer may legitimately contain several atomic services (for
-- example Botox and Dysport at the same per-unit member price).  The existing
-- normalizer checked "different item" before the per-unit branch and erased
-- these deterministic values.  Preserve package clearing for total-price
-- offers, but make the explicit per-unit semantics take precedence.
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

    IF NEW.service_id IS NULL
       OR v_discount_price IS NULL
       OR v_price_model = 'from' THEN
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

    IF v_has_different_item OR v_price_model <> 'total' THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    v_total_quantity := COALESCE(NEW.quantity, 0) + v_other_quantity;
    IF v_total_quantity <= 0 THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;
    v_derived_unit_price := round(v_discount_price / v_total_quantity, 4);
    NEW.unit_price := CASE
        WHEN v_derived_unit_price * v_total_quantity = v_discount_price
            THEN v_derived_unit_price
        ELSE NULL
    END;
    RETURN NEW;
END;
$function$;

-- For a per-unit offer, the offer's explicit per-unit sale price is the item
-- unit price.  This repairs only deterministic rows with a stated price.
UPDATE public.promo_offer_items AS item
   SET unit_price = offer.discount_price,
       updated_at = now()
  FROM public.promo_offer_master AS offer
 WHERE item.offer_id = offer.id
   AND offer.is_active
   AND offer.price_model = 'per_unit'
   AND offer.discount_price IS NOT NULL
   AND item.service_id IS NOT NULL
   AND item.unit_price IS NULL;

-- Evidence explicitly states the savings for this offer, so its regular price
-- is deterministically $700 + $100 = $800.  Apply the same safe rule to other
-- active offers whose text explicitly contains a savings amount.
UPDATE public.promo_offer_master AS offer
   SET regular_price = CASE
           WHEN offer_raw_text = 'Botox: 30% off at $9.45/unit; 20 units $189 vs $270 regular.' THEN 270
           WHEN offer_raw_text = 'Best Botox Ever with Dr Bonadkar. Minimum 70 units at $11 per unit - $770 (value $980)' THEN 980
       END,
       updated_at = now()
 WHERE offer.is_active
   AND offer.regular_price IS NULL;

UPDATE public.promo_offer_master
   SET regular_price = discount_price + discount_amount,
       updated_at = now()
 WHERE is_active
   AND regular_price IS NULL
   AND discount_price IS NOT NULL
   AND discount_amount IS NOT NULL
   AND offer_raw_text ~* '\m(save|saves|savings?)\M\s*\$\s*[0-9]';

-- This helper returns another user's unsubscribe bearer token when called with
-- an arbitrary email.  It is not a self-service RPC; backend mail processing
-- can continue through service_role while public API roles cannot invoke it.
REVOKE EXECUTE ON FUNCTION public.email_prefs_for_address(text)
    FROM PUBLIC, anon, authenticated;

COMMIT;
