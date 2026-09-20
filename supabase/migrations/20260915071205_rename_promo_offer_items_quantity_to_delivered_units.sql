BEGIN;

DO $$
DECLARE
    has_quantity boolean;
    has_delivered_units boolean;
BEGIN
    IF to_regclass('public.promo_offer_items') IS NULL THEN
        RAISE EXCEPTION 'promo_offer_items table is required';
    END IF;

    SELECT EXISTS (
               SELECT 1
                 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'promo_offer_items'
                  AND column_name = 'quantity'
           ),
           EXISTS (
               SELECT 1
                 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'promo_offer_items'
                  AND column_name = 'delivered_units'
           )
      INTO has_quantity, has_delivered_units;

    IF has_quantity AND NOT has_delivered_units THEN
        ALTER TABLE public.promo_offer_items
            RENAME COLUMN quantity TO delivered_units;
    ELSIF has_quantity = has_delivered_units THEN
        RAISE EXCEPTION
            'promo_offer_items must have exactly one of quantity or delivered_units';
    END IF;
END $$;

COMMENT ON COLUMN public.promo_offer_items.delivered_units IS
    'Positive stated delivered quantity only. Per-unit or unspecified amounts stay NULL.';

CREATE OR REPLACE FUNCTION public.deactivate_offer_with_unresolved_item()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'public'
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
                   AND round(NEW.unit_price * coalesce(NEW.delivered_units, 1), 2)
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

CREATE OR REPLACE FUNCTION public.fn_offer_items_signature(p_offer_id bigint)
RETURNS text
LANGUAGE sql
STABLE
SET search_path TO 'public'
AS $function$
  SELECT COALESCE(
    string_agg(
      COALESCE(i.service_id::text, 'null')
      || ':'
      || COALESCE(i.delivered_units::text, 'null')
      || ':'
      || public.fn_normalize_fingerprint_segment(i.item_name),
      ','
      ORDER BY
        i.service_id NULLS LAST,
        public.fn_normalize_fingerprint_segment(i.item_name),
        i.delivered_units NULLS LAST
    ),
    'no_items'
  )
  FROM public.promo_offer_items i
  WHERE i.offer_id = p_offer_id;
$function$;

CREATE OR REPLACE FUNCTION public.normalize_promo_offer_item_unit_price()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'public'
AS $function$
DECLARE
    v_is_package boolean;
    v_discount_price numeric;
    v_delivered_units numeric;
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

    v_delivered_units := coalesce(NEW.delivered_units, 1);
    NEW.unit_price := round(v_discount_price / v_delivered_units, 4);
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.prevent_incomplete_offer_activation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'public'
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
                  AND round(item.unit_price * coalesce(item.delivered_units, 1), 2)
                      IS DISTINCT FROM round(NEW.discount_price, 2)
            )
        )
    ) THEN
        NEW.is_active := false;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.sync_non_package_offer_prices(p_offer_id bigint)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path TO 'public'
AS $function$
DECLARE
    v_is_package boolean;
    v_discount_price numeric;
    v_item_count integer;
    v_item_id bigint;
    v_delivered_units numeric;
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
           max(coalesce(item.delivered_units, 1)),
           max(service.regular_price)
      INTO v_item_count, v_item_id, v_delivered_units, v_service_regular_price
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
           SET unit_price = round(v_discount_price / v_delivered_units, 4)
         WHERE offer_item_id = v_item_id
           AND unit_price IS DISTINCT FROM round(v_discount_price / v_delivered_units, 4);
    END IF;

    IF v_service_regular_price IS NOT NULL THEN
         UPDATE public.promo_offer_master
           SET regular_price = round(v_service_regular_price * v_delivered_units, 2)
         WHERE id = p_offer_id
           AND regular_price IS DISTINCT FROM round(v_service_regular_price * v_delivered_units, 2)
           AND (v_discount_price IS NULL OR
                round(v_discount_price, 2) <=
                round(v_service_regular_price * v_delivered_units, 2));
    END IF;
END;
$function$;

CREATE OR REPLACE FUNCTION public.sync_offer_regular_price_from_service()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'public'
AS $function$
BEGIN
    IF NEW.regular_price IS NULL THEN
        RETURN NEW;
    END IF;

    UPDATE public.promo_offer_master AS master
       SET regular_price = round(NEW.regular_price * coalesce(item.delivered_units, 1), 2)
      FROM public.promo_offer_items AS item
     WHERE item.offer_id = master.id
       AND item.service_id = NEW.service_id
       AND master.is_package = false
       AND (SELECT count(*)
              FROM public.promo_offer_items AS all_items
             WHERE all_items.offer_id = master.id) = 1
       AND master.regular_price IS DISTINCT FROM
           round(NEW.regular_price * coalesce(item.delivered_units, 1), 2)
       AND (master.discount_price IS NULL OR
            round(master.discount_price, 2) <=
            round(NEW.regular_price * coalesce(item.delivered_units, 1), 2));

    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.validate_non_package_offer_prices()
RETURNS trigger
LANGUAGE plpgsql
SET search_path TO 'public'
AS $function$
DECLARE
    v_offer_id bigint;
BEGIN
    IF TG_TABLE_NAME = 'clinic_services' THEN
        IF NEW.regular_price IS NULL THEN
            RETURN NULL;
        END IF;

        IF EXISTS (
            SELECT 1
              FROM public.promo_offer_master AS master
              JOIN public.promo_offer_items AS item
                ON item.offer_id = master.id
              JOIN public.clinic_services AS service
                ON service.service_id = item.service_id
             WHERE service.service_id = NEW.service_id
               AND master.is_package = false
               AND (SELECT count(*) FROM public.promo_offer_items AS all_items
                    WHERE all_items.offer_id = master.id) = 1
               AND (
                   (master.discount_price IS NOT NULL AND
                    round(master.discount_price, 2) IS DISTINCT FROM
                    round(item.unit_price * coalesce(item.delivered_units, 1), 2))
                   OR (
                       (master.discount_price IS NULL OR
                        round(master.discount_price, 2) <=
                        round(service.regular_price * coalesce(item.delivered_units, 1), 2))
                       AND round(master.regular_price, 2) IS DISTINCT FROM
                           round(service.regular_price * coalesce(item.delivered_units, 1), 2)
                   )
               )
        ) THEN
            RAISE EXCEPTION
                'non-package offer price invariant violated for service %',
                NEW.service_id;
        END IF;
        RETURN NULL;
    END IF;

    v_offer_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.offer_id ELSE NEW.offer_id END;

    IF EXISTS (
        SELECT 1
          FROM public.promo_offer_master AS master
          JOIN public.promo_offer_items AS item
            ON item.offer_id = master.id
          LEFT JOIN public.clinic_services AS service
            ON service.service_id = item.service_id
         WHERE master.id = v_offer_id
           AND master.is_package = false
           AND (SELECT count(*) FROM public.promo_offer_items AS all_items
                WHERE all_items.offer_id = master.id) = 1
           AND (
               (master.discount_price IS NOT NULL AND
                round(master.discount_price, 2) IS DISTINCT FROM
                round(item.unit_price * coalesce(item.delivered_units, 1), 2))
               OR (
                   service.regular_price IS NOT NULL
                   AND (master.discount_price IS NULL OR
                        round(master.discount_price, 2) <=
                        round(service.regular_price * coalesce(item.delivered_units, 1), 2))
                   AND round(master.regular_price, 2) IS DISTINCT FROM
                       round(service.regular_price * coalesce(item.delivered_units, 1), 2)
               )
           )
    ) THEN
        RAISE EXCEPTION
            'non-package offer price invariant violated for offer %',
            v_offer_id;
    END IF;

    RETURN NULL;
END;
$function$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'promo_offer_items'
           AND column_name = 'quantity'
    ) OR NOT EXISTS (
        SELECT 1
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = 'promo_offer_items'
           AND column_name = 'delivered_units'
    ) THEN
        RAISE EXCEPTION 'promo_offer_items delivered_units rename validation failed';
    END IF;
END $$;

COMMIT;
