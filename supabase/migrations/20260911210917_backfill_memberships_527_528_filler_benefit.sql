BEGIN;

-- Current source: https://www.bonakdaraesthetics.com/specials
INSERT INTO public.clinic_services (business_id, service_name, source_url)
SELECT 3173, 'Dermal Filler', 'https://www.bonakdaraesthetics.com/specials'
 WHERE NOT EXISTS (
     SELECT 1 FROM public.clinic_services
      WHERE business_id = 3173 AND lower(btrim(service_name)) = 'dermal filler'
 );

WITH source_offers (membership_plan_id, offer_raw_text) AS (
    VALUES
        (527::bigint, 'Botox VIP Club - Gold: $100 off any filler treatment done the same day'::text),
        (528::bigint, 'Botox VIP Club - Platinum: $100 off any filler treatment done the same day'::text)
)
INSERT INTO public.promo_offer_master (business_id, membership_plan_id, discount_amount, offer_raw_text, price_model, is_membership_required, is_active, moderation_status)
SELECT membership.business_id, source.membership_plan_id, 100, source.offer_raw_text, 'total', TRUE, TRUE, 'approved'
  FROM source_offers AS source
  JOIN public.clinic_memberships AS membership ON membership.plan_id = source.membership_plan_id
 WHERE NOT EXISTS (
     SELECT 1 FROM public.promo_offer_master AS existing
      WHERE existing.membership_plan_id = source.membership_plan_id AND existing.offer_raw_text = source.offer_raw_text
 );

WITH source_offers (membership_plan_id, offer_raw_text) AS (
    VALUES
        (527::bigint, 'Botox VIP Club - Gold: $100 off any filler treatment done the same day'::text),
        (528::bigint, 'Botox VIP Club - Platinum: $100 off any filler treatment done the same day'::text)
)
INSERT INTO public.promo_offer_items (offer_id, service_id, item_name, quantity, reason)
SELECT offer.id, service.service_id, 'Dermal Filler', NULL,
       'Same-day membership filler benefit; source does not state an item count. Quantity intentionally NULL.'
  FROM source_offers AS source
  JOIN public.promo_offer_master AS offer ON offer.membership_plan_id = source.membership_plan_id AND offer.offer_raw_text = source.offer_raw_text
  JOIN public.clinic_services AS service ON service.business_id = 3173 AND lower(btrim(service.service_name)) = 'dermal filler'
 WHERE NOT EXISTS (
     SELECT 1 FROM public.promo_offer_items AS item
      WHERE item.offer_id = offer.id AND item.service_id = service.service_id
 );

COMMIT;
