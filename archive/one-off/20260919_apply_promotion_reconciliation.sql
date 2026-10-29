BEGIN;

SELECT pg_advisory_xact_lock(hashtextextended('costfinder_promotion_reconciliation_20260919', 0));

CREATE TEMP TABLE target_promotions ON COMMIT DROP AS
SELECT promotion_id, updated_at
FROM public.clinic_promotions
WHERE promotion_id IN (
  14,28,57,138,180,186,198,212,216,218,232,671,697,811,850,857,860,
  943,982,3410,3416,3441,3494,3500,3503,3512,3513,3514,3543,3551,3564,
  3587,3591,3592,3594,3595
);

DO $$
DECLARE
  target_count integer;
BEGIN
  SELECT count(*) INTO target_count FROM target_promotions;
  IF target_count <> 36 THEN
    RAISE EXCEPTION 'promotion reconciliation precondition failed: expected 36 targets, got %', target_count;
  END IF;
END $$;

-- Source/status reconciliation. The content changes below only remove known
-- extraction noise or encode claims explicitly present in the execution plan.
UPDATE public.clinic_promotions AS p
SET source_url = CASE
      WHEN p.promotion_id = 28 THEN 'https://belleviemedical.com/monthly-specials'
      ELSE p.source_url
    END,
    is_active = CASE
      WHEN p.promotion_id IN (138,857,860,3564,3594) THEN false
      ELSE p.is_active
    END,
    campaign_end_date = CASE
      WHEN p.promotion_id = 3592 THEN NULL
      ELSE p.campaign_end_date
    END,
    promotion_content = CASE
      WHEN p.promotion_id = 14 THEN jsonb_build_array(
        'Get FREE Consultation & **$50** Credit On Your First Treatment.*',
        '_Regular $850,_ _New Guest Price **$499**, save $351_',
        'Allergan Botox, $7.50 per Unit',
        '$5.98 per unit, 50 unit minimum',
        '_Regular $1,480,_ _New Guest Price **$888**_'
      )
      WHEN p.promotion_id = 180 THEN jsonb_build_array(
        'Save up to 50% off all our Laser Services',
        'Save up to 50% off our Morpheus Services',
        'Refer a Friend and Get a $150 Gift Certificate'
      )
      WHEN p.promotion_id = 198 THEN jsonb_build_array(
        '$200 OFF Daxxify — new treatment area; 44 units minimum',
        'Tixel Special — $1,100 OFF',
        'FREE Exosomes or Salmon DNA infusion',
        'Refer a Friend: you and your friend get $50 off or new patient price, whichever is better'
      )
      WHEN p.promotion_id = 212 THEN jsonb_build_array(
        'New clients can get Botox® at $9/unit on their first visit. Minimum session $180.',
        '$50 off your first medical-grade Hydrafacial®.',
        'New Client $69 Skin Rejuvenation Service Offer',
        'New Client $200 SkinPen® Microneedling Offer',
        'Laser Hair Removal as Low as $50',
        'New Client $100 off first syringe of Juvederm®.'
      )
      WHEN p.promotion_id = 232 THEN jsonb_build_array(
        'Botox $10/unit',
        'Filler $100 off',
        'DiamondGlow $199'
      )
      WHEN p.promotion_id = 3416 THEN jsonb_build_array(
        'Dysport $50 off',
        'Filler dealer''s choice $75 off',
        'Xeomin $99 (regular $200)',
        'IPL: buy 1 area, get the second area of lesser value free',
        'Under-eye PRP/PRF $399',
        '20% off skincare'
      )
      WHEN p.promotion_id = 3494 THEN jsonb_build_array(
        'Sign up for promotions and updates and receive 10% off your next cosmetic treatment'
      )
      WHEN p.promotion_id = 3500 THEN jsonb_build_array(
        'Join the Clarebella Beauty Insider Club in September and receive an $80 Beauty Bonus',
        'Purchase 3+ full-size skincare products and enjoy 18% off'
      )
      WHEN p.promotion_id = 3512 THEN jsonb_build_array(
        '15% off first treatment via email signup'
      )
      WHEN p.promotion_id = 3513 THEN jsonb_build_array(
        '20% off Microneedling + PDRN Salmon DNA Catalyst Serum',
        'Give $100, get $100'
      )
      WHEN p.promotion_id = 3514 THEN jsonb_build_array(
        'Juvederm or Restylane 3-for-2 filler special: $1,200 (regular $1,800)'
      )
      WHEN p.promotion_id = 3543 THEN jsonb_build_array(
        'New patients only: Neurotoxin treatments for $8/unit with 50+ units',
        '10% off vitamin injections',
        'Fill out the form and receive 10% off any service'
      )
      WHEN p.promotion_id = 3591 THEN jsonb_build_array(
        'Walk-In Tox Days — Jeuveau $10/unit',
        'EVOLYSSE filler $699',
        'PRP + SkinPen microneedling + PDRN (salmon DNA) $599',
        'FREE Dermaplaning ($75 value) with any HydraFacial MD treatment',
        'FREE Booster ($50 value) with any HydraFacial MD treatment',
        'SkinVive BOGO: 2 syringes for $662'
      )
      WHEN p.promotion_id = 3592 THEN jsonb_build_array(
        'CO2 Laser, $500 off face',
        'RF Microneedling, $750 for face ($900 value)',
        'Filler, $50 off 1 syringe',
        'Hydrafacial add-on: complimentary Dermaplane ($150 value)',
        'AlloClae $500 off',
        'Brachioplasty $1,000 off',
        'New patient $50 off'
      )
      WHEN p.promotion_id = 943 THEN jsonb_build_array(
        '10% off first treatment',
        'New patients only',
        'Cannot be combined with other offers',
        'Expires 12/31/26'
      )
      WHEN p.promotion_id = 982 THEN jsonb_build_array(
        'Forehead TOX $79 (regular $135), 41% off, per area'
      )
      WHEN p.promotion_id = 697 THEN p.promotion_content || jsonb_build_array(
        'Face laser treatment — neck included free'
      )
      ELSE p.promotion_content
    END,
    is_offer_extracted = CASE
      WHEN p.promotion_id IN (232,3512,3514,3543,3591,3595) THEN true
      WHEN p.promotion_id IN (14,180,186,198,212,3410,3416,3441,3494,3500,3503,3513,3551,3587,3592,943,982,697) THEN false
      ELSE p.is_offer_extracted
    END,
    updated_at = now()
FROM target_promotions AS old
WHERE p.promotion_id = old.promotion_id
  AND p.updated_at = old.updated_at
  AND p.promotion_id IN (
    14,28,138,180,198,212,232,3416,3494,3500,3512,3513,3514,3543,3591,3592,
    943,982,697
  );

-- Existing Offer repairs. The trigger-backed item update below prevents a
-- total price from being interpreted as a per-unit price.
UPDATE public.promo_offer_master AS m
SET offer_raw_text = CASE
      WHEN m.id = 8119 THEN '$200 OFF Daxxify — new treatment area; minimum 44 units'
      WHEN m.id = 8017 THEN 'New clients receive Botox at $9/unit on the first visit; minimum session $180.'
      WHEN m.id = 8365 THEN 'New patient Botox $10/unit'
      WHEN m.id = 7979 THEN 'Sculptra Bio Stimulator x6: $2,990 vs $4,800.'
      WHEN m.id = 8228 THEN 'Forehead TOX | New Client Offer $49 (regular $135)'
      WHEN m.id = 8232 THEN 'Jawline Contour Filler | New Client Offer $399 (regular $599)'
      WHEN m.id = 8600 THEN 'Botox for excessive underarm sweating, 100 units, only $1,049 (regular $1,200).'
      WHEN m.id = 10180 THEN 'Walk-In Tox Days — SkinVive BOGO: buy one syringe, get the second 50% off; $662 for 2 syringes'
      ELSE m.offer_raw_text
    END,
    conditions = CASE
      WHEN m.id = 8119 THEN ARRAY['new treatment area', 'minimum 44 units']::text[]
      WHEN m.id = 8017 THEN ARRAY['new client first visit', 'minimum session $180']::text[]
      ELSE m.conditions
    END,
    price_model = CASE
      WHEN m.id = 8509 THEN 'total'
      WHEN m.id IN (8017,8365) THEN 'per_unit'
      ELSE m.price_model
    END,
    regular_price = CASE
      WHEN m.id = 8509 THEN 450
      WHEN m.id = 1427 THEN NULL
      WHEN m.id = 7979 THEN 4800
      WHEN m.id = 8232 THEN 599
      WHEN m.id = 8600 THEN 1200
      ELSE m.regular_price
    END,
    discount_price = CASE
      WHEN m.id = 8509 THEN 400
      WHEN m.id = 8017 THEN 9
      WHEN m.id = 8365 THEN 10
      WHEN m.id = 8228 THEN 49
      ELSE m.discount_price
    END,
    discount_percent = CASE
      WHEN m.id = 8228 THEN 63.70
      WHEN m.id = 8232 THEN 33.39
      WHEN m.id = 8600 THEN 12.58
      ELSE m.discount_percent
    END,
    discount_amount = CASE
      WHEN m.id = 8509 THEN 50
      WHEN m.id = 8600 THEN 151
      ELSE m.discount_amount
    END,
    is_package = CASE
      WHEN m.id IN (7979,8509,8600) THEN true
      WHEN m.id = 10180 THEN true
      ELSE m.is_package
    END,
    updated_at = now()
FROM (
  SELECT id, updated_at
  FROM public.promo_offer_master
  WHERE id IN (8119,8017,8365,8509,7979,8228,8232,8600,10180,1427)
) AS old
WHERE m.id = old.id
  AND m.updated_at = old.updated_at;

UPDATE public.promo_offer_items AS i
SET unit_price = CASE
      WHEN i.offer_id = 8017 THEN 9
      WHEN i.offer_id = 8365 THEN 10
      WHEN i.offer_id = 8509 THEN NULL
      WHEN i.offer_id = 8600 THEN NULL
      WHEN i.offer_id = 8228 THEN 49
      ELSE i.unit_price
    END,
    delivered_units = CASE
      WHEN i.offer_id IN (8017,8365) THEN NULL
      WHEN i.offer_id = 8509 THEN 120
      WHEN i.offer_id = 8600 THEN 100
      WHEN i.offer_id = 7979 THEN 6
      WHEN i.offer_id = 10180 THEN 2
      ELSE i.delivered_units
    END,
    updated_at = now()
WHERE i.offer_id IN (8017,8365,8509,7979,10180);

-- Keep only the canonical current-specials source and remove the unrelated
-- historical/duplicate Offer rows named in the plan.
UPDATE public.promo_offer_master
SET is_active = false, updated_at = now()
WHERE id IN (8492,8246,8247,10087)
  AND is_active;

-- Reuse already parsed Offer rows that have complete item mappings.
UPDATE public.promo_offer_master
SET is_active = true, updated_at = now()
WHERE id IN (
  8228,8229,8230,8231,8232,
  7978,7979,7980,
  8556,8559,8560,8562,8563,8566,8567,8568,8575,8576,8587,8589,8590,8594,8596,8599,8600,8602,
  10175,10180
)
AND NOT is_active;

-- Promotion 3512 has one canonical service in the live taxonomy. Attach the
-- existing signup Offer to it before activating; unresolved additional service
-- names stay in the review queue rather than being invented.
INSERT INTO public.promo_offer_items (offer_id, item_name, service_id, delivered_units, unit_price)
SELECT 9861, s.service_name, s.service_id, NULL, NULL
FROM public.clinic_services AS s
WHERE s.business_id = 2993
  AND s.service_name = 'Microneedling'
  AND NOT EXISTS (
    SELECT 1 FROM public.promo_offer_items i WHERE i.offer_id = 9861
  );
UPDATE public.promo_offer_master
SET is_active = true, updated_at = now()
WHERE id = 9861 AND NOT is_active;

CREATE TEMP TABLE new_offer_ids (offer_id bigint, service_id bigint) ON COMMIT DROP;

-- New Offer specs with canonical services currently present in the live DB.
DO $$
DECLARE
  spec record;
  new_id bigint;
  v_service_id bigint;
BEGIN
  FOR spec IN
    SELECT * FROM (VALUES
      (3543::bigint, 359::bigint, 'Vitamin Injection'::text, '10% off vitamin injections'::text, ARRAY['Form signup']::text[], NULL::numeric, NULL::numeric, 10::numeric, NULL::numeric, false, false),
      (3543::bigint, 359::bigint, 'Botox'::text, 'Fill out the form and receive 10% off any service'::text, ARRAY['Form signup', 'Any service']::text[], NULL::numeric, NULL::numeric, 10::numeric, NULL::numeric, false, false),
      (3543::bigint, 359::bigint, 'Neurotoxin'::text, 'Fill out the form and receive 10% off any service'::text, ARRAY['Form signup', 'Any service']::text[], NULL::numeric, NULL::numeric, 10::numeric, NULL::numeric, false, false),
      (3543::bigint, 359::bigint, 'Vitamin Injection'::text, 'Fill out the form and receive 10% off any service'::text, ARRAY['Form signup', 'Any service']::text[], NULL::numeric, NULL::numeric, 10::numeric, NULL::numeric, false, false)
    ) AS x(promotion_id,business_id,service_name,raw,conditions,regular_price,discount_price,discount_percent,discount_amount,is_package,is_new_customer_required)
  LOOP
    IF EXISTS (
      SELECT 1 FROM public.promo_offer_master m
      JOIN public.promo_offer_items i ON i.offer_id = m.id
      JOIN public.clinic_services s ON s.service_id = i.service_id
      WHERE m.promotion_id = spec.promotion_id
        AND m.offer_raw_text = spec.raw
        AND s.business_id = spec.business_id
        AND s.service_name::text = spec.service_name
    ) THEN
      CONTINUE;
    END IF;

    SELECT service_id INTO v_service_id
    FROM public.clinic_services
    WHERE business_id = spec.business_id AND service_name::text = spec.service_name
    ORDER BY service_id
    LIMIT 1;

    IF v_service_id IS NULL THEN
      CONTINUE;
    END IF;

    INSERT INTO public.promo_offer_master (
      business_id, promotion_id, is_active, is_package, price_model,
      regular_price, discount_price, discount_percent, discount_amount,
      is_membership_required, is_new_customer_required, offer_raw_text, conditions
    ) VALUES (
      spec.business_id, spec.promotion_id, false, spec.is_package, 'total',
      spec.regular_price, spec.discount_price, spec.discount_percent, spec.discount_amount,
      false, spec.is_new_customer_required, spec.raw, spec.conditions
    ) RETURNING id INTO new_id;

    INSERT INTO public.promo_offer_items (offer_id, item_name, service_id, delivered_units, unit_price)
    VALUES (new_id, spec.service_name, v_service_id, NULL, NULL);

    UPDATE public.promo_offer_master
    SET is_active = true, updated_at = now()
    WHERE id = new_id;

    INSERT INTO new_offer_ids VALUES (new_id, v_service_id);
  END LOOP;
END $$;

-- Recompute extracted flags only where the current active Offer set now covers
-- the explicitly reconciled content. Ambiguous or service-incomplete sources
-- intentionally remain false for the review queue.
UPDATE public.clinic_promotions
SET is_offer_extracted = true, updated_at = now()
WHERE promotion_id IN (232,3512,3514,3543,3591,3595)
  AND is_active;

DO $$
DECLARE
  bad_active bigint;
  bad_items bigint;
  bad_parent bigint;
  dup_fp bigint;
BEGIN
  SELECT count(*) INTO bad_active
  FROM public.promo_offer_master m
  WHERE m.is_active
    AND ((m.promotion_id IS NULL) = (m.membership_plan_id IS NULL));

  SELECT count(*) INTO bad_items
  FROM public.promo_offer_master m
  WHERE m.is_active
    AND NOT EXISTS (SELECT 1 FROM public.promo_offer_items i WHERE i.offer_id = m.id)
     OR (m.is_active AND EXISTS (
       SELECT 1 FROM public.promo_offer_items i
       LEFT JOIN public.clinic_services s ON s.service_id = i.service_id
       WHERE i.offer_id = m.id AND s.service_id IS NULL
     ));

  SELECT count(*) INTO bad_parent
  FROM public.promo_offer_master m
  JOIN public.clinic_promotions p ON p.promotion_id = m.promotion_id
  WHERE m.is_active AND m.promotion_id IS NOT NULL AND NOT p.is_active;

  SELECT count(*) INTO dup_fp
  FROM (
    SELECT offer_fingerprint
    FROM public.promo_offer_master
    WHERE is_active AND offer_fingerprint IS NOT NULL
    GROUP BY offer_fingerprint
    HAVING count(*) > 1
  ) duplicates;

  IF bad_active <> 0 OR bad_items <> 0 OR bad_parent <> 0 OR dup_fp <> 0 THEN
    RAISE EXCEPTION 'promotion reconciliation QA failed: invalid source %, bad items %, inactive parent %, duplicate fingerprints %', bad_active, bad_items, bad_parent, dup_fp;
  END IF;
END $$;

COMMIT;
