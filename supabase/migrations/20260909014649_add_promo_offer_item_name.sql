ALTER TABLE public.promo_offer_items
  ADD COLUMN item_name text;

COMMENT ON COLUMN public.promo_offer_items.item_name IS
  'Name of the service/item represented by this offer item. When service_id is present, use clinic_services.service_name; otherwise preserve the extracted item name from the offer.';

UPDATE public.promo_offer_items AS poi
SET item_name = cs.service_name,
    updated_at = now()
FROM public.clinic_services AS cs
WHERE poi.service_id = cs.service_id;
