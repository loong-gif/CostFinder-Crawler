BEGIN;

-- Current source: https://www.alluremedspa.com/monthly-specials
-- Each row below has an explicit sale and regular price on the September page.
INSERT INTO public.clinic_services (business_id, service_name, source_url)
SELECT source.business_id, source.service_name, source.source_url
  FROM (
      VALUES
          (3079::bigint, 'Letybo'::text, 'https://www.alluremedspa.com/monthly-specials'::text),
          (3079::bigint, 'Microneedling'::text, 'https://www.alluremedspa.com/monthly-specials'::text)
  ) AS source(business_id, service_name, source_url)
 WHERE NOT EXISTS (
     SELECT 1 FROM public.clinic_services AS existing
      WHERE existing.business_id = source.business_id
        AND lower(btrim(existing.service_name)) = lower(source.service_name)
 );

WITH source_offers (service_name, offer_raw_text, regular_price, discount_price, price_model, quantity, unit_price) AS (
    VALUES
        ('CO2 Laser'::text, 'CO2 Laser Face: $1100 (reg. price: $1250/treatment)'::text, 1250::numeric, 1100::numeric, 'total'::text, 1::smallint, NULL::numeric),
        ('CO2 Laser'::text, 'CO2 Face & Neck: $1400 (reg. price: $1750/treatment)'::text, 1750::numeric, 1400::numeric, 'total'::text, 1::smallint, NULL::numeric),
        ('Letybo'::text, 'Letybo: Only $8/unit (reg. price: $10/unit); minimum 30 units'::text, 10::numeric, 8::numeric, 'per_unit'::text, NULL::smallint, 8::numeric),
        ('Microneedling'::text, 'Microneedling: $650 (reg. price: $750/treatment)'::text, 750::numeric, 650::numeric, 'total'::text, 1::smallint, NULL::numeric)
)
INSERT INTO public.promo_offer_master (
    business_id, promotion_id, regular_price, discount_price, offer_raw_text,
    price_model, is_active, moderation_status
)
SELECT 3079, 217, source.regular_price, source.discount_price, source.offer_raw_text,
       source.price_model, TRUE, 'approved'
  FROM source_offers AS source
 WHERE NOT EXISTS (
     SELECT 1 FROM public.promo_offer_master AS existing
      WHERE existing.promotion_id = 217
        AND existing.offer_raw_text = source.offer_raw_text
 );

WITH source_offers (service_name, offer_raw_text, quantity, unit_price) AS (
    VALUES
        ('CO2 Laser'::text, 'CO2 Laser Face: $1100 (reg. price: $1250/treatment)'::text, 1::smallint, NULL::numeric),
        ('CO2 Laser'::text, 'CO2 Face & Neck: $1400 (reg. price: $1750/treatment)'::text, 1::smallint, NULL::numeric),
        ('Letybo'::text, 'Letybo: Only $8/unit (reg. price: $10/unit); minimum 30 units'::text, NULL::smallint, 8::numeric),
        ('Microneedling'::text, 'Microneedling: $650 (reg. price: $750/treatment)'::text, 1::smallint, NULL::numeric)
)
INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, unit_price)
SELECT offer.id, service.service_id, source.service_name, source.quantity, source.unit_price
  FROM source_offers AS source
  JOIN public.promo_offer_master AS offer
    ON offer.promotion_id = 217
   AND offer.offer_raw_text = source.offer_raw_text
  JOIN public.clinic_services AS service
    ON service.business_id = 3079
   AND lower(btrim(service.service_name)) = lower(source.service_name)
 WHERE NOT EXISTS (
     SELECT 1 FROM public.promo_offer_items AS existing
      WHERE existing.offer_id = offer.id
        AND existing.service_id = service.service_id
 );

-- Three evidence-backed rows already existed but were inactive and had the
-- per-unit model inherited from an earlier parse. The source calls each a
-- treatment, so retain it as one total-price item instead.
WITH corrected_offers (offer_raw_text, regular_price, discount_price) AS (
    VALUES
        ('CO2 Laser Face: $1100 (reg. price: $1250/treatment)'::text, 1250::numeric, 1100::numeric),
        ('CO2 Face & Neck: $1400 (reg. price: $1750/treatment)'::text, 1750::numeric, 1400::numeric),
        ('Microneedling: $650 (reg. price: $750/treatment)'::text, 750::numeric, 650::numeric)
)
UPDATE public.promo_offer_master AS offer
   SET regular_price = corrected.regular_price,
       discount_price = corrected.discount_price,
       price_model = 'total',
       is_active = TRUE,
       moderation_status = 'approved'
  FROM corrected_offers AS corrected
 WHERE offer.promotion_id = 217
   AND offer.offer_raw_text = corrected.offer_raw_text;

UPDATE public.promo_offer_items AS item
   SET quantity = 1,
       unit_price = NULL,
       updated_at = now()
  FROM public.promo_offer_master AS offer
 WHERE item.offer_id = offer.id
   AND offer.promotion_id = 217
   AND offer.offer_raw_text IN (
       'CO2 Laser Face: $1100 (reg. price: $1250/treatment)',
       'CO2 Face & Neck: $1400 (reg. price: $1750/treatment)',
       'Microneedling: $650 (reg. price: $750/treatment)'
   );

-- The old Microneedling item had no service mapping; the mapped row above is
-- the same offer and service text, so retain only the source-backed item.
DELETE FROM public.promo_offer_items AS item
 WHERE item.offer_id IN (
     SELECT id FROM public.promo_offer_master
      WHERE promotion_id = 217
        AND offer_raw_text = 'Microneedling: $650 (reg. price: $750/treatment)'
 )
   AND item.service_id IS NULL
   AND item.item_name = 'Microneedling'
   AND EXISTS (
       SELECT 1 FROM public.promo_offer_items AS mapped
        WHERE mapped.offer_id = item.offer_id
          AND mapped.service_id IS NOT NULL
          AND mapped.item_name = item.item_name
   );

-- Keep the treatment count independent of the quoted dollar amount.
UPDATE public.promo_offer_items AS item
   SET quantity = 1,
       updated_at = now()
  FROM public.promo_offer_master AS offer
 WHERE item.offer_id = offer.id
   AND offer.promotion_id = 217
   AND offer.offer_raw_text = 'Microneedling: $650 (reg. price: $750/treatment)'
   AND item.item_name = 'Microneedling'
   AND item.quantity IS DISTINCT FROM 1;

COMMIT;
