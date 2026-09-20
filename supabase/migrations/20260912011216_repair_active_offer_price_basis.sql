-- Repair the four active offers identified by the 2026-09-11 price-integrity review.
-- Match the source-backed raw text, not generated row ids, so this remains auditable.

-- 7647: the source only supports a total sale price. The legacy amount/percent
-- fields had no regular-price basis and were internally contradictory.
UPDATE public.promo_offer_master
SET regular_price = NULL,
    discount_amount = NULL,
    discount_percent = NULL,
    price_model = 'total'
WHERE is_active
  AND offer_raw_text = 'Series of 3 | $2,400'
  AND discount_price = 2400;

-- 7649: replace the stale extraction with the current Xeomin source statement.
UPDATE public.promo_offer_master
SET offer_raw_text = 'Our 50-unit Xeomin bundle is $450 (just $9.00/unit). That''s about 18% less than the equivalent Botox treatment.',
    regular_price = 11,
    discount_price = 9,
    discount_amount = 2,
    discount_percent = 18.18,
    price_model = 'per_unit'
WHERE is_active
  AND offer_raw_text = 'Our 50 unit Xeomin bundle is $600 (just $12.00/unit). That''s about 14% less than the equivalent Botox treatment.'
  AND discount_price = 12;

-- The current source is a Xeomin bundle, not a Botox/Xeomin bundle.
DELETE FROM public.promo_offer_items AS item
WHERE item.offer_id IN (
    SELECT offer.id
    FROM public.promo_offer_master AS offer
    WHERE offer.offer_raw_text = 'Our 50-unit Xeomin bundle is $450 (just $9.00/unit). That''s about 18% less than the equivalent Botox treatment.'
)
AND item.service_id IN (
    SELECT service.service_id
    FROM public.clinic_services AS service
    WHERE lower(service.service_name) = 'botox'
);

UPDATE public.promo_offer_items AS item
SET quantity = 50,
    unit_price = 9,
    item_name = service.service_name
FROM public.promo_offer_master AS offer,
     public.clinic_services AS service
WHERE item.offer_id = offer.id
  AND service.service_id = item.service_id
  AND offer.offer_raw_text = 'Our 50-unit Xeomin bundle is $450 (just $9.00/unit). That''s about 18% less than the equivalent Botox treatment.'
  AND lower(service.service_name) = 'xeomin';

-- 7665: retain the per-unit price; the separate total $50 saving cannot occupy
-- per-unit discount fields without a regular per-unit basis.
UPDATE public.promo_offer_master
SET regular_price = NULL,
    discount_amount = NULL,
    discount_percent = NULL,
    price_model = 'per_unit'
WHERE is_active
  AND offer_raw_text = 'Purchase Dysport $4.30 per unit Get $50 off (minimum 120 units)'
  AND discount_price = 4.30;

-- 8418: represent the explicitly stated rate on a per-unit basis. The 25-unit
-- minimum is a threshold, not a fixed quantity delivered by the offer.
UPDATE public.promo_offer_master
SET regular_price = 15,
    discount_price = 9,
    discount_amount = 6,
    discount_percent = 40,
    price_model = 'per_unit'
WHERE is_active
  AND offer_raw_text = 'Xeomin injection: $9/unit with 25 units or more; normally $15/unit.'
  AND discount_price = 225;

UPDATE public.promo_offer_items AS item
SET unit_price = 9,
    item_name = service.service_name
FROM public.promo_offer_master AS offer,
     public.clinic_services AS service
WHERE item.offer_id = offer.id
  AND service.service_id = item.service_id
  AND offer.offer_raw_text = 'Xeomin injection: $9/unit with 25 units or more; normally $15/unit.'
  AND lower(service.service_name) = 'xeomin';
