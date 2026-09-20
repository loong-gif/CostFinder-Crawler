BEGIN;

-- Current source: https://newportbeachmedspa.com/membership
WITH source_offers (membership_plan_id, offer_raw_text) AS (
    VALUES
        (488::bigint, 'RF-MicroNeedling Membership: 10% off all services'::text),
        (489::bigint, 'Velashape III Membership: 10% off all services'::text),
        (490::bigint, 'ProNox Membership: 10% off all services'::text)
)
INSERT INTO public.promo_offer_master (business_id, membership_plan_id, discount_percent, offer_raw_text, price_model, is_membership_required, is_active, moderation_status)
SELECT membership.business_id, source.membership_plan_id, 10, source.offer_raw_text, 'total', TRUE, TRUE, 'approved'
  FROM source_offers AS source
  JOIN public.clinic_memberships AS membership ON membership.plan_id = source.membership_plan_id
 WHERE NOT EXISTS (
     SELECT 1 FROM public.promo_offer_master AS existing
      WHERE existing.membership_plan_id = source.membership_plan_id
        AND existing.offer_raw_text = source.offer_raw_text
 );

WITH source_offers (membership_plan_id, offer_raw_text) AS (
    VALUES
        (488::bigint, 'RF-MicroNeedling Membership: 10% off all services'::text),
        (489::bigint, 'Velashape III Membership: 10% off all services'::text),
        (490::bigint, 'ProNox Membership: 10% off all services'::text)
)
INSERT INTO public.promo_offer_items (offer_id, item_name, quantity, reason)
SELECT offer.id, 'All services', NULL,
       'Membership-wide percent benefit; no atomic service mapping. Quantity intentionally NULL.'
  FROM source_offers AS source
  JOIN public.promo_offer_master AS offer ON offer.membership_plan_id = source.membership_plan_id AND offer.offer_raw_text = source.offer_raw_text
 WHERE NOT EXISTS (
     SELECT 1 FROM public.promo_offer_items AS item
      WHERE item.offer_id = offer.id AND item.service_id IS NULL AND item.item_name = 'All services'
 );

UPDATE public.promo_offer_items AS item
   SET reason = 'Membership-wide percent benefit; no atomic service mapping. Quantity intentionally NULL.',
       updated_at = now()
  FROM public.promo_offer_master AS offer
 WHERE item.offer_id = offer.id
   AND offer.membership_plan_id IN (488, 489, 490)
   AND item.item_name = 'All services'
   AND item.quantity IS NULL;

COMMIT;
