-- Source-backed offer quality repair. Matching includes old values and
-- updated_at so a changed row aborts instead of being overwritten.
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('costfinder_offer_quality_repair_20260912', 0));

CREATE TEMP TABLE offer_quality_plan (
    business_id bigint NOT NULL,
    offer_raw_text text NOT NULL,
    old_regular_price numeric,
    old_discount_price numeric,
    old_discount_amount numeric,
    old_discount_percent numeric,
    old_updated_at timestamptz NOT NULL,
    new_regular_price numeric,
    new_discount_price numeric,
    new_discount_amount numeric,
    new_discount_percent numeric,
    plan_kind text NOT NULL
) ON COMMIT DROP;

INSERT INTO offer_quality_plan VALUES
    (241, 'Botox/Jeuveau - Staff $9.50 per unit with 40 unit minimum Save $2.50 per unit off regular price', NULL, NULL, 2.50, NULL, '2026-09-12 07:43:53.875971+00', 12, 9.50, 2.50, 20.83, 'duplicate'),
    (241, 'Botox/Jeuveau - Staff $9.50 per unit with 40 unit minimum Save $2.50 per unit off regular price', 12, 9.50, 2.50, 20.83, '2026-09-12 07:43:53.875971+00', 12, 9.50, 2.50, 20.83, 'repair'),
    (1318, '$11/u Dysport or Botox ($13/u for non-members)', NULL, NULL, 2, 15.38, '2026-09-12 07:43:53.875971+00', 13, 11, 2, 15.38, 'repair'),
    (3068, '$9 per Botox® unit*,*For all units beyond the first 20 units of Botox®', NULL, NULL, NULL, 40, '2026-09-12 07:43:53.875971+00', NULL, 9, NULL, NULL, 'duplicate'),
    (241, 'Botox/Jeuveau - Staff $9.50 per unit with 40 unit minimum Save $2.50 per unit off regular price', 480, 380, 100, 20.83, '2026-09-12 07:43:53.875971+00', 12, 9.50, 2.50, 20.83, 'duplicate'),
    (241, 'Botox/Jeuveau - Doctor $12.00 per unit with 40 unit minimum Save $2.00 per unit off regular price', 560, 480, 80, 14.29, '2026-09-12 07:43:53.875971+00', 14, 12, 2, 14.29, 'duplicate'),
    (1318, '$10/u Jeuveau, Letybo, or Xeomin ($12/u for non-members)', NULL, NULL, 2, 16.67, '2026-09-12 07:43:53.875971+00', 12, 10, 2, 16.67, 'repair'),
    (2199, 'Botox/Dysport $12.80/unit', NULL, NULL, 2.20, 14.67, '2026-09-12 07:43:53.875971+00', NULL, 12.80, NULL, NULL, 'repair'),
    (1318, '$11.50/u Dysport or Botox ($13/u for non-members)', NULL, NULL, 1.50, 11.54, '2026-09-12 07:43:53.875971+00', 13, 11.50, 1.50, 11.54, 'repair'),
    (1318, '$10.50/u Jeuveau, Letybo, or Xeomin ($12/u for non-members)', NULL, NULL, 1.50, 12.50, '2026-09-12 07:43:53.875971+00', 12, 10.50, 1.50, 12.50, 'repair'),
    (1406, '$11 a unit Botox', NULL, NULL, 4, 26.67, '2026-09-12 07:43:53.875971+00', NULL, 11, NULL, NULL, 'repair'),
    (3066, 'Exclusive Injectable Pricing: Botox Cosmetic - $11/unit (40-unit minimum)', NULL, NULL, 4, 26.67, '2026-09-12 07:43:53.875971+00', NULL, 11, NULL, NULL, 'repair'),
    (3040, '$14/unit on Botox', NULL, NULL, 1, 6.67, '2026-09-12 07:43:53.875971+00', NULL, 14, NULL, NULL, 'repair'),
    (1713, '10% off Dermal Fillers, Lash & Brow Services', NULL, NULL, NULL, 10, '2026-09-12 08:06:32.823608+00', NULL, NULL, NULL, NULL, 'duplicate'),
    (241, 'Jeuveau – $9.00/unit', NULL, 9, NULL, NULL, '2026-09-12 08:04:03.009805+00', NULL, 9, NULL, NULL, 'repair'),
    (241, 'Botox – $9.50/unit', NULL, 9.50, NULL, NULL, '2026-09-12 08:04:03.589902+00', NULL, 9.50, NULL, NULL, 'repair'),
    (246, 'Lip Filler — Juvéderm Ultra XC: $379 per syringe', NULL, 379, NULL, NULL, '2026-09-12 08:04:07.709215+00', NULL, 379, NULL, NULL, 'repair'),
    (246, 'Chin Filler — Radiesse 1.0cc: $299 per syringe', NULL, 299, NULL, NULL, '2026-09-12 08:04:08.277492+00', NULL, 299, NULL, NULL, 'repair'),
    (3066, 'Dysport® — $11/unit (40-unit minimum)', 13, 11, 2, 15.38, '2026-09-12 07:43:53.875971+00', 13, 11, 2, 15.38, 'repair'),
    (3094, '$13/unit Botox (regularly $14/unit)', 14, 13, 1, 7.14, '2026-09-12 08:05:36.478625+00', 14, 13, 1, 7.14, 'repair'),
    (3095, 'Botox $10.50/unit (reg. $12.50/unit)', 12.50, 10.50, 2, 16, '2026-09-12 07:43:53.875971+00', 12.50, 10.50, 2, 16, 'repair'),
    (3095, 'Dysport $3.75/unit (reg. $4.75/unit)', 4.75, 3.75, 1, 21.05, '2026-09-12 07:43:53.875971+00', 4.75, 3.75, 1, 21.05, 'repair'),
    (3095, 'Xeomin $9/unit (reg. $11/unit)', 11, 9, 2, 18.18, '2026-09-12 07:43:53.875971+00', 11, 9, 2, 18.18, 'repair'),
    (2227, 'We offer tiered pricing based on the number of units purchased. Botox and Dysport range from $14 to $18 per unit. The more units you purchase, the more you save.', NULL, NULL, 4, 22.22, '2026-09-12 07:43:53.875971+00', NULL, 18, NULL, NULL, 'review'),
    (2227, 'We offer tiered pricing based on the number of units purchased. Botox and Dysport range from $14 to $18 per unit. For Dysport, 3 units are equivalent to 1 Botox unit. The more units you purchase, the more you save.', NULL, 14, NULL, NULL, '2026-09-12 07:43:53.875971+00', NULL, 14, NULL, NULL, 'review'),
    (2995, 'Tox Treatments $1 off per unit', NULL, NULL, 1, NULL, '2026-09-12 08:05:00.921781+00', NULL, 1, NULL, NULL, 'review'),
    (2995, 'Tox Treatments $2 off per unit', NULL, NULL, 2, NULL, '2026-09-12 08:05:04.408141+00', NULL, 2, NULL, NULL, 'review'),
    (1219, '12% off Botox / Dysport ($12.32/unit Botox & $4.10/unit Dysport!)', NULL, 12.32, NULL, 12, '2026-09-12 08:06:28.696785+00', NULL, 12.32, NULL, NULL, 'review'),
    (1713, '$1 off per unit of Botox, Jeuveau, and Daxxify', NULL, NULL, 1, NULL, '2026-09-12 08:06:36.219176+00', NULL, 1, NULL, NULL, 'review'),
    (3176, 'Tox: $1 off per unit for 12 months', NULL, NULL, 1, NULL, '2026-09-12 08:07:49.254569+00', NULL, 1, NULL, NULL, 'review'),
    (3176, 'Tox: $2 off per unit for 12 months', NULL, NULL, 2, NULL, '2026-09-12 08:07:53.927142+00', NULL, 2, NULL, NULL, 'review'),
    (3257, '$10/unit toxin', NULL, 10, NULL, NULL, '2026-09-12 07:43:53.875971+00', NULL, 10, NULL, NULL, 'review'),
    (3257, '$11/unit toxin', NULL, 11, NULL, NULL, '2026-09-12 07:43:53.875971+00', NULL, 11, NULL, NULL, 'review'),
    (3014, '$50 Off with Aspire Rewards (Reg. $3.75/unit. Minimum 120 units)', NULL, NULL, 50, NULL, '2026-09-12 07:43:53.875971+00', NULL, 3.75, NULL, NULL, 'review'),
    (346, 'Tox as low as $9/unit + additional discounts on select toxins', NULL, 9, NULL, NULL, '2026-09-12 08:03:46.094364+00', NULL, 9, NULL, NULL, 'review'),
    (246, 'Naked Glow+ — Radiesse + PRF: $499 per treatment', NULL, 499, NULL, NULL, '2026-09-12 08:04:10.051547+00', NULL, 499, NULL, NULL, 'review'),
    (246, 'Naked Glow — Sculptra + PRF: $599 per treatment', NULL, 599, NULL, NULL, '2026-09-12 08:04:10.634040+00', NULL, 599, NULL, NULL, 'review'),
    (3257, '$12/unit toxin', NULL, 12, NULL, NULL, '2026-09-12 07:43:53.875971+00', NULL, 12, NULL, NULL, 'review'),
    (2217, 'Dermal Filler: $650 per syringe; buy 2 syringes and save 30% with code FILLER30', NULL, NULL, NULL, 30, '2026-09-12 07:43:53.875971+00', NULL, 650, NULL, NULL, 'review'),
    (2238, 'Facial Balancing: 30% off second syringe at $700/syringe', 700, NULL, NULL, 30, '2026-09-12 07:43:53.875971+00', 700, 700, NULL, NULL, 'review'),
    (2995, 'Dysport $0.50 off per unit', NULL, NULL, 0.50, NULL, '2026-09-12 08:05:01.514524+00', NULL, 0.50, NULL, NULL, 'review'),
    (2995, 'Dysport $1 off per unit', NULL, NULL, 1, NULL, '2026-09-12 08:05:05.010988+00', NULL, 1, NULL, NULL, 'review'),
    (1713, '$1 off per unit of Botox, Jeuveau, and Daxxify', NULL, NULL, 1, NULL, '2026-09-12 08:06:36.795648+00', NULL, 1, NULL, NULL, 'review'),
    (1713, '$1 off per unit of Botox, Jeuveau, and Daxxify', NULL, NULL, 1, NULL, '2026-09-12 08:06:37.405061+00', NULL, 1, NULL, NULL, 'review');

CREATE TEMP TABLE offer_quality_item_remap (
    business_id bigint NOT NULL,
    offer_raw_text text NOT NULL,
    from_service_name text NOT NULL,
    to_service_name text NOT NULL
) ON COMMIT DROP;
INSERT INTO offer_quality_item_remap VALUES
    (1318, '$10/u Jeuveau, Letybo, or Xeomin ($12/u for non-members)', 'Botox', 'Letybo'),
    (241, 'Jeuveau – $9.00/unit', 'Botox', 'Jeuveau'),
    (246, 'Lip Filler — Juvéderm Ultra XC: $379 per syringe', 'Dermal Filler', 'Lip Filler'),
    (246, 'Chin Filler — Radiesse 1.0cc: $299 per syringe', 'Dermal Filler', 'Chin Filler Radiesse'),
    (3066, 'Dysport® — $11/unit (40-unit minimum)', 'Botox', 'Dysport'),
    (3095, 'Dysport $3.75/unit (reg. $4.75/unit)', 'Botox', 'Dysport'),
    (3095, 'Xeomin $9/unit (reg. $11/unit)', 'Botox', 'Xeomin');

DO $function$
DECLARE
    repair_matches integer;
    duplicate_matches integer;
    review_matches integer;
    remap_matches integer;
BEGIN
    SELECT count(*) INTO repair_matches FROM offer_quality_plan p JOIN public.promo_offer_master m
      ON m.business_id=p.business_id AND m.offer_raw_text=p.offer_raw_text
     AND m.regular_price IS NOT DISTINCT FROM p.old_regular_price AND m.discount_price IS NOT DISTINCT FROM p.old_discount_price
     AND m.discount_amount IS NOT DISTINCT FROM p.old_discount_amount AND m.discount_percent IS NOT DISTINCT FROM p.old_discount_percent
     AND m.updated_at=p.old_updated_at AND m.is_active WHERE p.plan_kind='repair';
    SELECT count(*) INTO duplicate_matches FROM offer_quality_plan p JOIN public.promo_offer_master m
      ON m.business_id=p.business_id AND m.offer_raw_text=p.offer_raw_text
     AND m.regular_price IS NOT DISTINCT FROM p.old_regular_price AND m.discount_price IS NOT DISTINCT FROM p.old_discount_price
     AND m.discount_amount IS NOT DISTINCT FROM p.old_discount_amount AND m.discount_percent IS NOT DISTINCT FROM p.old_discount_percent
     AND m.updated_at=p.old_updated_at AND m.is_active WHERE p.plan_kind='duplicate';
    SELECT count(*) INTO review_matches FROM offer_quality_plan p JOIN public.promo_offer_master m
      ON m.business_id=p.business_id AND m.offer_raw_text=p.offer_raw_text
     AND m.regular_price IS NOT DISTINCT FROM p.old_regular_price AND m.discount_price IS NOT DISTINCT FROM p.old_discount_price
     AND m.discount_amount IS NOT DISTINCT FROM p.old_discount_amount AND m.discount_percent IS NOT DISTINCT FROM p.old_discount_percent
     AND m.updated_at=p.old_updated_at AND m.is_active WHERE p.plan_kind='review';
    SELECT count(*) INTO remap_matches FROM public.promo_offer_items i
      JOIN public.promo_offer_master m ON m.id=i.offer_id
      JOIN offer_quality_item_remap r ON r.business_id=m.business_id AND r.offer_raw_text=m.offer_raw_text
      JOIN public.clinic_services old_service ON old_service.service_id=i.service_id AND old_service.business_id=m.business_id
       AND lower(old_service.service_name)=lower(r.from_service_name)
      JOIN public.clinic_services new_service ON new_service.business_id=m.business_id
       AND lower(new_service.service_name)=lower(r.to_service_name)
     WHERE m.is_active;
    IF repair_matches<>19 OR duplicate_matches<>5 OR review_matches<>21 OR remap_matches<>7 THEN
        RAISE EXCEPTION 'offer quality repair precondition failed: repair %, duplicate %, review %, remap %', repair_matches, duplicate_matches, review_matches, remap_matches;
    END IF;
END;
$function$;

UPDATE public.promo_offer_master m
SET is_active=false, moderation_status='pending_review',
    moderation_notes=concat_ws(E'\n', NULLIF(m.moderation_notes,''), 'QA 2026-09-12: duplicate source offer; retained inactive in favor of the complete source row'), updated_at=now()
FROM offer_quality_plan p
WHERE p.plan_kind='duplicate' AND m.business_id=p.business_id AND m.offer_raw_text=p.offer_raw_text
  AND m.regular_price IS NOT DISTINCT FROM p.old_regular_price AND m.discount_price IS NOT DISTINCT FROM p.old_discount_price
  AND m.discount_amount IS NOT DISTINCT FROM p.old_discount_amount AND m.discount_percent IS NOT DISTINCT FROM p.old_discount_percent
  AND m.updated_at=p.old_updated_at AND m.is_active;

UPDATE public.promo_offer_master m
SET is_active=false, moderation_status='pending_review',
    moderation_notes=concat_ws(E'\n', NULLIF(m.moderation_notes,''), 'QA 2026-09-12: ambiguous per-unit price basis; held for manual review'), updated_at=now()
FROM offer_quality_plan p
WHERE p.plan_kind='review' AND m.business_id=p.business_id AND m.offer_raw_text=p.offer_raw_text
  AND m.regular_price IS NOT DISTINCT FROM p.old_regular_price AND m.discount_price IS NOT DISTINCT FROM p.old_discount_price
  AND m.discount_amount IS NOT DISTINCT FROM p.old_discount_amount AND m.discount_percent IS NOT DISTINCT FROM p.old_discount_percent
  AND m.updated_at=p.old_updated_at AND m.is_active;

UPDATE public.promo_offer_master m
SET regular_price=p.new_regular_price, discount_price=p.new_discount_price,
    discount_amount=p.new_discount_amount, discount_percent=p.new_discount_percent, price_model='per_unit', updated_at=now()
FROM offer_quality_plan p
WHERE p.plan_kind='repair' AND m.business_id=p.business_id AND m.offer_raw_text=p.offer_raw_text
  AND m.regular_price IS NOT DISTINCT FROM p.old_regular_price AND m.discount_price IS NOT DISTINCT FROM p.old_discount_price
  AND m.discount_amount IS NOT DISTINCT FROM p.old_discount_amount AND m.discount_percent IS NOT DISTINCT FROM p.old_discount_percent
  AND m.updated_at=p.old_updated_at AND m.is_active;

UPDATE public.promo_offer_items i
SET service_id=new_service.service_id, item_name=new_service.service_name, unit_price=m.discount_price, updated_at=now()
FROM public.promo_offer_master m, offer_quality_item_remap r, public.clinic_services old_service, public.clinic_services new_service
WHERE i.offer_id=m.id AND r.business_id=m.business_id AND r.offer_raw_text=m.offer_raw_text
  AND old_service.business_id=m.business_id AND lower(old_service.service_name)=lower(r.from_service_name)
  AND new_service.business_id=m.business_id AND lower(new_service.service_name)=lower(r.to_service_name)
  AND i.service_id=old_service.service_id AND m.is_active;

UPDATE public.promo_offer_items i
SET unit_price=m.discount_price, item_name=service.service_name, updated_at=now()
FROM public.promo_offer_master m, offer_quality_plan p, public.clinic_services service
WHERE i.offer_id=m.id AND p.plan_kind='repair' AND p.business_id=m.business_id AND p.offer_raw_text=m.offer_raw_text
  AND service.service_id=i.service_id AND service.business_id=m.business_id AND m.discount_price IS NOT NULL;

UPDATE public.promo_offer_items i
SET item_name=service.service_name, updated_at=now()
FROM public.promo_offer_master m, public.clinic_services service
WHERE i.offer_id=m.id AND service.service_id=i.service_id AND service.business_id=m.business_id
  AND i.item_name IS DISTINCT FROM service.service_name;

UPDATE public.promo_offer_master m
SET moderation_status='pending_review',
    moderation_notes=concat_ws(E'\n', NULLIF(m.moderation_notes,''), 'QA 2026-09-12: invalid source relation; retained inactive for manual review'), updated_at=now()
WHERE NOT m.is_active AND (m.promotion_id IS NULL)=(m.membership_plan_id IS NULL)
  AND coalesce(m.moderation_notes,'') NOT LIKE '%QA 2026-09-12: invalid source relation%';

COMMIT;
