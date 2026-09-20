-- Active membership/promotion offers must have a complete service-item set.
CREATE OR REPLACE FUNCTION public.prevent_incomplete_offer_activation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
DECLARE
    item_count integer;
BEGIN
    IF NEW.is_active THEN
        SELECT count(*) INTO item_count
        FROM public.promo_offer_items AS item
        WHERE item.offer_id = NEW.id;

        IF item_count = 0 OR EXISTS (
            SELECT 1
            FROM public.promo_offer_items AS item
            LEFT JOIN public.clinic_services AS service
              ON service.service_id = item.service_id
            WHERE item.offer_id = NEW.id
              AND (item.service_id IS NULL OR service.service_id IS NULL
                   OR service.business_id IS DISTINCT FROM NEW.business_id)
        ) THEN
            NEW.is_active := false;
            NEW.moderation_status := COALESCE(NEW.moderation_status, 'pending_review');
            NEW.moderation_notes := COALESCE(NEW.moderation_notes || E'\n', '')
                || 'Offer requires at least one same-business mapped service item';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_prevent_incomplete_offer_activation
    ON public.promo_offer_master;
CREATE TRIGGER trg_prevent_incomplete_offer_activation
BEFORE INSERT OR UPDATE OF is_active ON public.promo_offer_master
FOR EACH ROW EXECUTE FUNCTION public.prevent_incomplete_offer_activation();
