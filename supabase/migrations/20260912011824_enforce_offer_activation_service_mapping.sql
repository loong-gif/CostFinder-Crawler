-- Prevent a later import step from reactivating an offer whose items remain
-- unresolved when the import writes the master row last.
CREATE OR REPLACE FUNCTION public.prevent_unresolved_offer_activation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    IF NEW.is_active AND EXISTS (
        SELECT 1
        FROM public.promo_offer_items AS item
        WHERE item.offer_id = NEW.id
          AND item.service_id IS NULL
    ) THEN
        NEW.is_active := false;
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_prevent_unresolved_offer_activation
    ON public.promo_offer_master;
CREATE TRIGGER trg_prevent_unresolved_offer_activation
BEFORE INSERT OR UPDATE OF is_active ON public.promo_offer_master
FOR EACH ROW
EXECUTE FUNCTION public.prevent_unresolved_offer_activation();

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
