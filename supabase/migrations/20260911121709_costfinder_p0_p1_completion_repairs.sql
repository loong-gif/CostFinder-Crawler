BEGIN;

-- ``private.is_admin()`` remains available to authenticated RLS policies, but
-- neither helper may be reached through the public PostgREST RPC surface.
REVOKE EXECUTE ON FUNCTION public.is_admin() FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION private.is_admin() FROM PUBLIC, anon;

-- The source pages explicitly provide these sale and regular-price pairs.
-- fn_auto_fix_offer_prices derives the two comparison fields on update.
WITH corrected_prices (id, regular_price, discount_price) AS (
    VALUES
        (1503::bigint, 499::numeric, 399::numeric),
        (7399::bigint, 799::numeric, 699::numeric),
        (7401::bigint, 499::numeric, 399::numeric),
        (7711::bigint, 799::numeric, 699::numeric),
        (7720::bigint, 599::numeric, 499::numeric),
        (7723::bigint, 899::numeric, 799::numeric),
        (7746::bigint, 135::numeric, 79::numeric),
        (7751::bigint, 135::numeric, 99::numeric),
        (8228::bigint, 135::numeric, 49::numeric),
        (8230::bigint, 499::numeric, 299::numeric),
        (8231::bigint, 499::numeric, 399::numeric)
)
UPDATE public.promo_offer_master AS offer
   SET regular_price = corrected_prices.regular_price,
       discount_price = corrected_prices.discount_price
  FROM corrected_prices
 WHERE offer.id = corrected_prices.id;

-- Re-read evidence showed the three legacy labels did not describe the actual
-- offer text. Add the source-backed atomic services instead of mapping them to
-- unrelated Botox/Laser rows; the last item is BBL, not a duplicate laser row.
INSERT INTO public.clinic_services (business_id, service_name, source_url)
SELECT source.business_id, source.service_name, source.source_url
  FROM (
      VALUES
          (2989::bigint, 'Signature Facial'::text, 'https://mybiospa.com/specials-events/holiday-beauty-day'::text),
          (3067::bigint, 'Custom Facial'::text, 'https://bluewaterwellnesscenter.com/promotions'::text),
          (3184::bigint, 'BBL'::text, 'https://foreveragelessinc.com/membership'::text)
  ) AS source(business_id, service_name, source_url)
 WHERE NOT EXISTS (
     SELECT 1
     FROM public.clinic_services AS existing
     WHERE existing.business_id = source.business_id
       AND lower(btrim(existing.service_name)) = lower(source.service_name)
 );

WITH exact_mappings (offer_item_id, business_id, service_name) AS (
    VALUES
        (1430::bigint, 2989::bigint, 'Signature Facial'::text),
        (1590::bigint, 3067::bigint, 'Custom Facial'::text),
        (2345::bigint, 3184::bigint, 'BBL'::text)
)
UPDATE public.promo_offer_items AS item
   SET service_id = service.service_id,
       item_name = service.service_name,
       reason = 'Deterministic source-backed same-business service mapping.',
       updated_at = now()
  FROM exact_mappings
  JOIN public.clinic_services AS service
    ON service.business_id = exact_mappings.business_id
   AND lower(btrim(service.service_name)) = lower(exact_mappings.service_name)
 WHERE item.offer_item_id = exact_mappings.offer_item_id
   AND item.service_id IS NULL;

-- Keep non-deterministic service identities unlinked, but make their review
-- category explicit. No cross-business or synonym inference occurs here.
UPDATE public.promo_offer_items AS item
   SET reason = 'No deterministic same-business exact service match; retain raw item name.',
       updated_at = now()
  FROM public.promo_offer_master AS offer
 WHERE offer.id = item.offer_id
   AND offer.is_active
   AND item.service_id IS NULL
   AND COALESCE(btrim(item.reason), '') = ''
   AND NOT EXISTS (
       SELECT 1
       FROM public.clinic_services AS service
       WHERE service.business_id = offer.business_id
         AND lower(btrim(regexp_replace(service.service_name, '[^a-zA-Z0-9]+', ' ', 'g')))
             = lower(btrim(regexp_replace(item.item_name, '[^a-zA-Z0-9]+', ' ', 'g')))
   );

-- Explicitly stated item quantity takes precedence over the default rule.
UPDATE public.promo_offer_items
   SET quantity = 10,
       reason = COALESCE(reason || ' ', '') || 'Quantity 10 explicitly stated by source.',
       updated_at = now()
 WHERE offer_item_id = 1775
   AND quantity IS NULL;

-- These are single, mapped, total-price atomic offers with no source quantity
-- or variable-quantity semantics. ``1`` denotes one item occurrence only.
UPDATE public.promo_offer_items
   SET quantity = 1,
       reason = COALESCE(reason || ' ', '') || 'Default quantity 1 for a single atomic item.',
       updated_at = now()
 WHERE offer_item_id = ANY (ARRAY[
     2373::bigint, 2392::bigint, 2399::bigint, 2415::bigint,
     2435::bigint, 2436::bigint, 2437::bigint, 2438::bigint,
     2441::bigint, 2442::bigint, 2487::bigint
 ])
   AND quantity IS NULL;

COMMIT;
