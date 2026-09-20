BEGIN;

-- Current source: https://skinmaison.com/beauty-bank
WITH source_offers (offer_raw_text, discount_amount, discount_percent, price_model, item_name, reason) AS (
    VALUES
        ('Deluxe Hydrafacial Membership: $2 off per unit Tox Treatments'::text, 2::numeric, NULL::numeric, 'per_unit'::text, 'Tox Treatments'::text, 'Per-unit membership benefit; no single toxin service was named.'::text),
        ('Deluxe Hydrafacial Membership: $100 off Injectables'::text, 100::numeric, NULL::numeric, 'total'::text, 'Injectables'::text, 'Membership-wide injectable benefit; no atomic service mapping.'::text),
        ('Deluxe Hydrafacial Membership: 10% off Skin Treatments'::text, NULL::numeric, 10::numeric, 'total'::text, 'Skin Treatments'::text, 'Membership-wide skin-treatment benefit; no atomic service mapping.'::text)
)
INSERT INTO public.promo_offer_master (business_id, membership_plan_id, discount_amount, discount_percent, offer_raw_text, price_model, is_membership_required, is_active, moderation_status)
SELECT membership.business_id, 521, source.discount_amount, source.discount_percent, source.offer_raw_text, source.price_model, TRUE, TRUE, 'approved'
  FROM source_offers AS source
  JOIN public.clinic_memberships AS membership ON membership.plan_id = 521
 WHERE NOT EXISTS (
     SELECT 1 FROM public.promo_offer_master AS existing
      WHERE existing.membership_plan_id = 521 AND existing.offer_raw_text = source.offer_raw_text
 );

WITH source_offers (offer_raw_text, item_name, reason) AS (
    VALUES
        ('Deluxe Hydrafacial Membership: $2 off per unit Tox Treatments'::text, 'Tox Treatments'::text, 'Per-unit membership benefit; no single toxin service was named.'::text),
        ('Deluxe Hydrafacial Membership: $100 off Injectables'::text, 'Injectables'::text, 'Membership-wide injectable benefit; no atomic service mapping.'::text),
        ('Deluxe Hydrafacial Membership: 10% off Skin Treatments'::text, 'Skin Treatments'::text, 'Membership-wide skin-treatment benefit; no atomic service mapping.'::text)
)
INSERT INTO public.promo_offer_items (offer_id, item_name, quantity, reason)
SELECT offer.id, source.item_name, NULL, source.reason || ' Quantity intentionally NULL.'
  FROM source_offers AS source
  JOIN public.promo_offer_master AS offer ON offer.membership_plan_id = 521 AND offer.offer_raw_text = source.offer_raw_text
 WHERE NOT EXISTS (
     SELECT 1 FROM public.promo_offer_items AS item
      WHERE item.offer_id = offer.id AND item.service_id IS NULL AND item.item_name = source.item_name
 );

COMMIT;
