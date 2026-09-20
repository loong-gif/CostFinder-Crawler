BEGIN;

CREATE OR REPLACE FUNCTION public.validate_non_package_offer_prices()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
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
                    round(item.unit_price * coalesce(item.quantity, 1), 2))
                   OR (
                       (master.discount_price IS NULL OR
                        round(master.discount_price, 2) <=
                        round(service.regular_price * coalesce(item.quantity, 1), 2))
                       AND round(master.regular_price, 2) IS DISTINCT FROM
                           round(service.regular_price * coalesce(item.quantity, 1), 2)
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
                round(item.unit_price * coalesce(item.quantity, 1), 2))
               OR (
                   service.regular_price IS NOT NULL
                   AND (master.discount_price IS NULL OR
                        round(master.discount_price, 2) <=
                        round(service.regular_price * coalesce(item.quantity, 1), 2))
                   AND round(master.regular_price, 2) IS DISTINCT FROM
                       round(service.regular_price * coalesce(item.quantity, 1), 2)
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

REVOKE ALL ON FUNCTION public.validate_non_package_offer_prices()
    FROM PUBLIC, anon, authenticated;

DO $$
DECLARE
    v_discount_mismatches bigint;
    v_regular_mismatches bigint;
BEGIN
    SELECT count(*)
      INTO v_discount_mismatches
      FROM public.promo_offer_master AS master
      JOIN public.promo_offer_items AS item
        ON item.offer_id = master.id
     WHERE master.is_package = false
       AND (SELECT count(*) FROM public.promo_offer_items AS all_items
            WHERE all_items.offer_id = master.id) = 1
       AND master.discount_price IS NOT NULL
       AND round(master.discount_price, 2) IS DISTINCT FROM
           round(item.unit_price * coalesce(item.quantity, 1), 2);

    SELECT count(*)
      INTO v_regular_mismatches
      FROM public.promo_offer_master AS master
      JOIN public.promo_offer_items AS item
        ON item.offer_id = master.id
      JOIN public.clinic_services AS service
        ON service.service_id = item.service_id
     WHERE master.is_package = false
       AND (SELECT count(*) FROM public.promo_offer_items AS all_items
            WHERE all_items.offer_id = master.id) = 1
       AND service.regular_price IS NOT NULL
       AND (master.discount_price IS NULL OR
            round(master.discount_price, 2) <=
            round(service.regular_price * coalesce(item.quantity, 1), 2))
       AND round(master.regular_price, 2) IS DISTINCT FROM
           round(service.regular_price * coalesce(item.quantity, 1), 2);

    IF v_discount_mismatches <> 0 OR v_regular_mismatches <> 0 THEN
        RAISE EXCEPTION
            'offer price invariant preflight failed: discount mismatches %, regular mismatches %',
            v_discount_mismatches, v_regular_mismatches;
    END IF;
END;
$$;

DROP TRIGGER IF EXISTS trg_validate_non_package_offer_prices_on_master
    ON public.promo_offer_master;
CREATE CONSTRAINT TRIGGER trg_validate_non_package_offer_prices_on_master
AFTER INSERT OR UPDATE OF discount_price, regular_price, is_package
ON public.promo_offer_master
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION public.validate_non_package_offer_prices();

DROP TRIGGER IF EXISTS trg_validate_non_package_offer_prices_on_item
    ON public.promo_offer_items;
CREATE CONSTRAINT TRIGGER trg_validate_non_package_offer_prices_on_item
AFTER INSERT OR DELETE OR UPDATE OF offer_id, service_id, quantity, unit_price
ON public.promo_offer_items
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION public.validate_non_package_offer_prices();

DROP TRIGGER IF EXISTS trg_validate_non_package_offer_prices_on_service
    ON public.clinic_services;
CREATE CONSTRAINT TRIGGER trg_validate_non_package_offer_prices_on_service
AFTER UPDATE OF regular_price ON public.clinic_services
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION public.validate_non_package_offer_prices();

COMMIT;
