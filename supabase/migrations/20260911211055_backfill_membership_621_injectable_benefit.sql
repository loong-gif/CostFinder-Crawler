BEGIN;

-- Current source: https://drsavalia.com/memberships
INSERT INTO public.promo_offer_master (business_id, membership_plan_id, discount_percent, offer_raw_text, price_model, is_membership_required, is_active, moderation_status)
SELECT membership.business_id, 621, 10,
       'Balanced Belle: 10% off injectables including Skinvive, Sculptra, and PRF',
       'total', TRUE, TRUE, 'approved'
  FROM public.clinic_memberships AS membership
 WHERE membership.plan_id = 621
   AND NOT EXISTS (
       SELECT 1 FROM public.promo_offer_master AS existing
        WHERE existing.membership_plan_id = 621
          AND existing.offer_raw_text = 'Balanced Belle: 10% off injectables including Skinvive, Sculptra, and PRF'
   );

INSERT INTO public.promo_offer_items (offer_id, item_name, quantity, reason)
SELECT offer.id, 'Injectables', NULL,
       'Membership-wide injectable benefit; no atomic service mapping. Quantity intentionally NULL.'
  FROM public.promo_offer_master AS offer
 WHERE offer.membership_plan_id = 621
   AND offer.offer_raw_text = 'Balanced Belle: 10% off injectables including Skinvive, Sculptra, and PRF'
   AND NOT EXISTS (
       SELECT 1 FROM public.promo_offer_items AS item
        WHERE item.offer_id = offer.id AND item.service_id IS NULL AND item.item_name = 'Injectables'
   );

COMMIT;
