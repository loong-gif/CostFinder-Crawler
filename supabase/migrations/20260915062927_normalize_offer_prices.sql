BEGIN;

COMMENT ON COLUMN public.promo_offer_master.price_model IS
    'Source expression only: total = source stated a whole-offer price; per_unit = source stated a unit price; from = source stated a starting price. Stored discount_price is always the normalized offer total, and promo_offer_items.unit_price is always the normalized item price.';

-- The normalized price contract is deliberately independent of price_model:
-- discount_price is the offer total, while unit_price is the total divided by
-- the quantity. Packages are excluded because one total cannot be allocated
-- across heterogeneous items without another pricing rule.
CREATE OR REPLACE FUNCTION public.sync_non_package_offer_prices(p_offer_id bigint)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $function$
DECLARE
    v_is_package boolean;
    v_discount_price numeric;
    v_item_count integer;
    v_item_id bigint;
    v_quantity numeric;
    v_service_regular_price numeric;
BEGIN
    SELECT is_package, discount_price
      INTO v_is_package, v_discount_price
      FROM public.promo_offer_master
     WHERE id = p_offer_id
     FOR UPDATE;

    IF NOT FOUND OR v_is_package THEN
        RETURN;
    END IF;

    SELECT count(*),
           max(item.offer_item_id),
           max(coalesce(item.quantity, 1)),
           max(service.regular_price)
      INTO v_item_count, v_item_id, v_quantity, v_service_regular_price
      FROM public.promo_offer_items AS item
      LEFT JOIN public.clinic_services AS service
        ON service.service_id = item.service_id
     WHERE item.offer_id = p_offer_id;

    IF v_item_count <> 1 THEN
        RETURN;
    END IF;

    IF v_discount_price IS NULL THEN
        UPDATE public.promo_offer_items
           SET unit_price = NULL
         WHERE offer_item_id = v_item_id
           AND unit_price IS NOT NULL;
    ELSE
        UPDATE public.promo_offer_items
           SET unit_price = round(v_discount_price / v_quantity, 4)
         WHERE offer_item_id = v_item_id
           AND unit_price IS DISTINCT FROM round(v_discount_price / v_quantity, 4);
    END IF;

    -- regular_price has one source of truth: the linked service's unit price.
    -- When the service price is unknown, leave any historical master value in
    -- place for QA instead of inventing a replacement.
    IF v_service_regular_price IS NOT NULL THEN
         UPDATE public.promo_offer_master
           SET regular_price = round(v_service_regular_price * v_quantity, 2)
         WHERE id = p_offer_id
           AND regular_price IS DISTINCT FROM round(v_service_regular_price * v_quantity, 2)
           AND (v_discount_price IS NULL OR
                round(v_discount_price, 2) <=
                round(v_service_regular_price * v_quantity, 2));
    END IF;
END;
$function$;

REVOKE ALL ON FUNCTION public.sync_non_package_offer_prices(bigint)
    FROM PUBLIC, anon, authenticated;

CREATE OR REPLACE FUNCTION public.normalize_promo_offer_item_unit_price()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
DECLARE
    v_is_package boolean;
    v_discount_price numeric;
    v_quantity numeric;
BEGIN
    SELECT is_package, discount_price
      INTO v_is_package, v_discount_price
      FROM public.promo_offer_master
     WHERE id = NEW.offer_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'No promo_offer_master found for offer_id=%', NEW.offer_id;
    END IF;

    IF v_is_package OR v_discount_price IS NULL THEN
        NEW.unit_price := NULL;
        RETURN NEW;
    END IF;

    v_quantity := coalesce(NEW.quantity, 1);
    NEW.unit_price := round(v_discount_price / v_quantity, 4);
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.sync_promo_offer_item_unit_prices()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $function$
BEGIN
    PERFORM public.sync_non_package_offer_prices(NEW.id);
    RETURN NULL;
END;
$function$;

REVOKE ALL ON FUNCTION public.sync_promo_offer_item_unit_prices()
    FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_sync_promo_offer_item_unit_prices
    ON public.promo_offer_master;
CREATE TRIGGER trg_sync_promo_offer_item_unit_prices
AFTER UPDATE OF discount_price, price_model ON public.promo_offer_master
FOR EACH ROW
WHEN (
    OLD.discount_price IS DISTINCT FROM NEW.discount_price
    OR OLD.price_model IS DISTINCT FROM NEW.price_model
)
EXECUTE FUNCTION public.sync_promo_offer_item_unit_prices();

CREATE OR REPLACE FUNCTION public.sync_non_package_offer_prices_from_item()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM public.sync_non_package_offer_prices(OLD.offer_id);
        RETURN NULL;
    END IF;

    PERFORM public.sync_non_package_offer_prices(NEW.offer_id);
    IF TG_OP = 'UPDATE' AND OLD.offer_id IS DISTINCT FROM NEW.offer_id THEN
        PERFORM public.sync_non_package_offer_prices(OLD.offer_id);
    END IF;
    RETURN NULL;
END;
$function$;

REVOKE ALL ON FUNCTION public.sync_non_package_offer_prices_from_item()
    FROM PUBLIC, anon, authenticated;

DROP TRIGGER IF EXISTS trg_z_sync_non_package_offer_prices
    ON public.promo_offer_items;
CREATE TRIGGER trg_z_sync_non_package_offer_prices
AFTER INSERT OR DELETE OR UPDATE OF offer_id, service_id, quantity
ON public.promo_offer_items
FOR EACH ROW
EXECUTE FUNCTION public.sync_non_package_offer_prices_from_item();

CREATE OR REPLACE FUNCTION public.sync_offer_regular_price_from_service()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    IF NEW.regular_price IS NULL THEN
        RETURN NEW;
    END IF;

    UPDATE public.promo_offer_master AS master
       SET regular_price = round(NEW.regular_price * coalesce(item.quantity, 1), 2)
      FROM public.promo_offer_items AS item
     WHERE item.offer_id = master.id
       AND item.service_id = NEW.service_id
       AND master.is_package = false
       AND (SELECT count(*)
              FROM public.promo_offer_items AS all_items
             WHERE all_items.offer_id = master.id) = 1
       AND master.regular_price IS DISTINCT FROM
           round(NEW.regular_price * coalesce(item.quantity, 1), 2)
       AND (master.discount_price IS NULL OR
            round(master.discount_price, 2) <=
            round(NEW.regular_price * coalesce(item.quantity, 1), 2));

    RETURN NEW;
END;
$function$;

REVOKE ALL ON FUNCTION public.sync_offer_regular_price_from_service()
    FROM PUBLIC, anon, authenticated;

-- Existing activation guards must use the normalized total/unit relationship;
-- otherwise a per_unit source row with quantity > 1 is incorrectly closed.
CREATE OR REPLACE FUNCTION public.prevent_incomplete_offer_activation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    IF NEW.is_active AND (
        (NEW.promotion_id IS NULL) = (NEW.membership_plan_id IS NULL)
        OR (NEW.promotion_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.clinic_promotions AS promotion
            WHERE promotion.promotion_id = NEW.promotion_id
              AND promotion.business_id = NEW.business_id
              AND promotion.is_active
              AND (promotion.campaign_end_date IS NULL
                   OR promotion.campaign_end_date >= current_date)
        ))
        OR (NEW.membership_plan_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.clinic_memberships AS membership
            WHERE membership.plan_id = NEW.membership_plan_id
              AND membership.business_id = NEW.business_id
        ))
        OR NOT EXISTS (
            SELECT 1 FROM public.promo_offer_items AS item
            WHERE item.offer_id = NEW.id
        )
        OR EXISTS (
            SELECT 1
              FROM public.promo_offer_items AS item
              LEFT JOIN public.clinic_services AS service
                ON service.service_id = item.service_id
             WHERE item.offer_id = NEW.id
               AND (item.service_id IS NULL
                    OR service.service_id IS NULL
                    OR service.business_id IS DISTINCT FROM NEW.business_id
                    OR item.item_name IS DISTINCT FROM service.service_name::text)
        )
        OR (
            NEW.is_package = false
            AND NEW.discount_price IS NOT NULL
            AND (SELECT count(*) FROM public.promo_offer_items AS item
                 WHERE item.offer_id = NEW.id) = 1
            AND EXISTS (
                SELECT 1 FROM public.promo_offer_items AS item
                WHERE item.offer_id = NEW.id
                  AND round(item.unit_price * coalesce(item.quantity, 1), 2)
                      IS DISTINCT FROM round(NEW.discount_price, 2)
            )
        )
    ) THEN
        NEW.is_active := false;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.deactivate_offer_with_unresolved_item()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM public.promo_offer_master AS offer
          LEFT JOIN public.clinic_services AS service
            ON service.service_id = NEW.service_id
         WHERE offer.id = NEW.offer_id
           AND offer.is_active
           AND (
               NEW.service_id IS NULL
               OR service.service_id IS NULL
               OR service.business_id IS DISTINCT FROM offer.business_id
               OR NEW.item_name IS DISTINCT FROM service.service_name::text
               OR (
                   offer.is_package = false
                   AND offer.discount_price IS NOT NULL
                   AND (SELECT count(*) FROM public.promo_offer_items AS item
                        WHERE item.offer_id = offer.id) = 1
                   AND round(NEW.unit_price * coalesce(NEW.quantity, 1), 2)
                       IS DISTINCT FROM round(offer.discount_price, 2)
               )
           )
    ) THEN
        UPDATE public.promo_offer_master
           SET is_active = false,
               updated_at = now()
         WHERE id = NEW.offer_id
           AND is_active;
    END IF;
    RETURN NEW;
END;
$function$;

-- Keep old regular values for per-unit rows whose linked service is still
-- unknown. The immediate master price check cannot allow a unit regular value
-- to coexist with the newly normalized total discount during this migration.
CREATE TEMP TABLE offer_price_source_conflicts ON COMMIT DROP AS
SELECT master.id AS offer_id
  FROM public.promo_offer_master AS master
  JOIN public.promo_offer_items AS item
    ON item.offer_id = master.id
  JOIN public.clinic_services AS service
    ON service.service_id = item.service_id
 WHERE master.is_package = false
   AND master.discount_price IS NOT NULL
   AND service.regular_price IS NOT NULL
   AND (SELECT count(*) FROM public.promo_offer_items AS all_items
        WHERE all_items.offer_id = master.id) = 1
   AND round(master.discount_price *
             CASE WHEN master.price_model = 'per_unit'
                  THEN coalesce(item.quantity, 1) ELSE 1 END, 2)
       > round(service.regular_price * coalesce(item.quantity, 1), 2);

CREATE TEMP TABLE offer_price_repair_originals ON COMMIT DROP AS
SELECT master.id AS offer_id,
       master.regular_price AS old_regular_price
  FROM public.promo_offer_master AS master
  JOIN public.promo_offer_items AS item
    ON item.offer_id = master.id
 WHERE master.is_package = false
   AND master.price_model = 'per_unit'
   AND item.quantity > 1
   AND NOT EXISTS (
       SELECT 1 FROM public.promo_offer_items AS other
       WHERE other.offer_id = item.offer_id
         AND other.offer_item_id <> item.offer_item_id
   );

-- These source pairs cannot satisfy both the normalized price formulas and the
-- existing discount<=regular check. Quarantine them instead of overwriting a
-- non-null service price with an inferred value.
UPDATE public.promo_offer_master AS master
   SET regular_price = NULL,
       is_active = false
  FROM offer_price_source_conflicts AS conflict
 WHERE conflict.offer_id = master.id;

-- Normalize old per-unit source rows into total master prices before deriving
-- every item price. This is data-driven and intentionally has no offer IDs.
UPDATE public.promo_offer_master AS master
   SET regular_price = NULL,
       discount_price = round(master.discount_price * item.quantity, 2)
  FROM public.promo_offer_items AS item
 WHERE item.offer_id = master.id
   AND master.is_package = false
   AND master.price_model = 'per_unit'
   AND item.quantity > 1
   AND NOT EXISTS (
       SELECT 1 FROM public.promo_offer_items AS other
       WHERE other.offer_id = item.offer_id
         AND other.offer_item_id <> item.offer_item_id
   );

-- Fill only unambiguous service prices from historical offer totals. Conflicts
-- remain untouched for QA rather than choosing an arbitrary value.
WITH candidates AS (
    SELECT item.service_id,
           round(coalesce(master.regular_price, original.old_regular_price)
                 / coalesce(item.quantity, 1), 2)
               AS implied_unit_price
      FROM public.promo_offer_master AS master
      JOIN public.promo_offer_items AS item
        ON item.offer_id = master.id
      JOIN public.clinic_services AS service
        ON service.service_id = item.service_id
      LEFT JOIN offer_price_repair_originals AS original
        ON original.offer_id = master.id
     WHERE master.is_package = false
       AND service.regular_price IS NULL
       AND coalesce(master.regular_price, original.old_regular_price) IS NOT NULL
       AND (SELECT count(*) FROM public.promo_offer_items AS all_items
            WHERE all_items.offer_id = master.id) = 1
), unambiguous AS (
    SELECT service_id, max(implied_unit_price) AS regular_price
      FROM candidates
     GROUP BY service_id
    HAVING count(DISTINCT implied_unit_price) = 1
)
UPDATE public.clinic_services AS service
   SET regular_price = unambiguous.regular_price
  FROM unambiguous
 WHERE service.service_id = unambiguous.service_id
   AND service.regular_price IS NULL;

-- Service backfill can expose additional conflicts on offers that had no
-- service price before. Quarantine those rows before rebuilding master
-- regular totals so the legacy discount<=regular check remains satisfiable.
UPDATE public.promo_offer_master AS master
   SET regular_price = NULL,
       is_active = false
  FROM public.promo_offer_items AS item
  JOIN public.clinic_services AS service
    ON service.service_id = item.service_id
 WHERE item.offer_id = master.id
   AND master.is_package = false
   AND master.discount_price IS NOT NULL
   AND service.regular_price IS NOT NULL
   AND (SELECT count(*) FROM public.promo_offer_items AS all_items
        WHERE all_items.offer_id = master.id) = 1
   AND round(master.discount_price, 2) >
       round(service.regular_price * coalesce(item.quantity, 1), 2);

-- Rebuild normalized regular totals from the service source of truth.
UPDATE public.promo_offer_master AS master
   SET regular_price = round(service.regular_price * coalesce(item.quantity, 1), 2)
  FROM public.promo_offer_items AS item
  JOIN public.clinic_services AS service
    ON service.service_id = item.service_id
 WHERE item.offer_id = master.id
   AND master.is_package = false
   AND service.regular_price IS NOT NULL
   AND (master.discount_price IS NULL OR
        round(master.discount_price, 2) <=
        round(service.regular_price * coalesce(item.quantity, 1), 2))
   AND NOT EXISTS (
       SELECT 1 FROM offer_price_source_conflicts AS conflict
       WHERE conflict.offer_id = master.id
   )
   AND (SELECT count(*) FROM public.promo_offer_items AS all_items
        WHERE all_items.offer_id = master.id) = 1;

-- Rebuild every normalized discount unit price, including NULL quantity = 1.
UPDATE public.promo_offer_items AS item
   SET unit_price = CASE
       WHEN master.discount_price IS NULL THEN NULL
       ELSE round(master.discount_price / coalesce(item.quantity, 1), 4)
   END
  FROM public.promo_offer_master AS master
 WHERE master.id = item.offer_id
   AND master.is_package = false
   AND (SELECT count(*) FROM public.promo_offer_items AS all_items
        WHERE all_items.offer_id = master.id) = 1;

-- Install propagation only after the historical service-price backfill has
-- finished, so that cleanup cannot observe an intermediate source value.
DROP TRIGGER IF EXISTS trg_sync_offer_regular_price_from_service
    ON public.clinic_services;
CREATE TRIGGER trg_sync_offer_regular_price_from_service
AFTER UPDATE OF regular_price ON public.clinic_services
FOR EACH ROW
EXECUTE FUNCTION public.sync_offer_regular_price_from_service();

COMMIT;
