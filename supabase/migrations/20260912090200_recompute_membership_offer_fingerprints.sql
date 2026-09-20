-- Align existing active membership offers with the plan-scoped fingerprint
-- function. A read-only precheck found no expected active collisions.
WITH desired AS (
    SELECT m.id,
           public.fn_build_offer_fingerprint(
               public.fn_offer_source_key(m.membership_plan_id, m.promotion_id, m.business_id),
               public.fn_offer_items_signature(m.id),
               m.discount_price,
               m.discount_amount,
               m.discount_percent
           ) AS offer_fingerprint
    FROM public.promo_offer_master m
    WHERE m.is_active AND m.membership_plan_id IS NOT NULL
)
UPDATE public.promo_offer_master m
SET offer_fingerprint = desired.offer_fingerprint,
    updated_at = now()
FROM desired
WHERE m.id = desired.id
  AND m.offer_fingerprint IS DISTINCT FROM desired.offer_fingerprint;
