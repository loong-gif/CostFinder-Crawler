-- Keep the public active set consistent when an import writes an unresolved
-- offer item after the initial master row has been created.
CREATE OR REPLACE FUNCTION public.deactivate_offer_with_unresolved_item()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $function$
BEGIN
    IF NEW.service_id IS NULL THEN
        UPDATE public.promo_offer_master
        SET is_active = false,
            updated_at = now()
        WHERE id = NEW.offer_id
          AND is_active;
    END IF;
    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS trg_deactivate_offer_with_unresolved_item
    ON public.promo_offer_items;
CREATE TRIGGER trg_deactivate_offer_with_unresolved_item
AFTER INSERT OR UPDATE OF service_id ON public.promo_offer_items
FOR EACH ROW
EXECUTE FUNCTION public.deactivate_offer_with_unresolved_item();

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
