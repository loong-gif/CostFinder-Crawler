BEGIN;

SELECT pg_advisory_xact_lock(hashtextextended('costfinder_promotion_lifecycle_review_20260919', 0));

DO $$
DECLARE
  expected_count integer := 78;
  actual_count integer;
BEGIN
  SELECT count(*) INTO actual_count
  FROM public.clinic_promotions
  WHERE NOT is_active;
  IF actual_count <> expected_count THEN
    RAISE EXCEPTION 'inactive promotion review set changed: expected %, got %', expected_count, actual_count;
  END IF;
END $$;

-- These pages currently show a usable promotion. Their old offer rows stay
-- inactive until the current page content is parsed and reconciled again.
UPDATE public.clinic_promotions
SET is_active = true,
    is_offer_extracted = false,
    is_ocr_required = false,
    requires_cf_verification = false,
    updated_at = now()
WHERE promotion_id IN (810, 857, 860, 3588, 3589, 3590)
  AND NOT is_active;

UPDATE public.clinic_promotions
SET promotion_title = 'Seasonal Specials | OC Skin Care Center Newport Beach',
    promotion_content = jsonb_build_array(
      'SPA SPECIALS',
      '10% Discount on your first product order'
    ),
    is_active = true,
    is_offer_extracted = false,
    is_ocr_required = false,
    requires_cf_verification = false,
    updated_at = now()
WHERE promotion_id = 810;

UPDATE public.clinic_promotions
SET promotion_title = 'Medical Spa Specials & Exclusive Offers | California Cosmetics',
    campaign_start_date = '2026-09-01',
    campaign_end_date = '2026-09-30',
    promotion_content = jsonb_build_array(
      'WALK-IN TOX DAYS — Jeuveau $10/unit',
      'EVOLYSSE filler: $699',
      'PRP with SkinPen microneedling + PDRN (salmon DNA): $599',
      'FREE Dermaplaning ($75 value) or Booster ($50 value) with any HydraFacial MD treatment',
      'SkinVive BOGO: 2 syringes for $662'
    ),
    updated_at = now()
WHERE promotion_id = 857;

UPDATE public.clinic_promotions
SET campaign_start_date = '2026-09-01',
    campaign_end_date = '2026-09-30',
    promotion_content = jsonb_build_array(
      'Post Procedure Kit — September only',
      'Post Procedure Kit only $259 (savings of $340)',
      'Hydra Balance Skin Saver',
      'Clarify Booster Skin Saver',
      'Skin-nourishing elixir with epidermal growth factor',
      'Anti-inflammatory antioxidant signature serum',
      'Hexapeptides formulation for post-procedure care'
    ),
    updated_at = now()
WHERE promotion_id = 860;

UPDATE public.clinic_promotions
SET campaign_start_date = '2026-09-01',
    campaign_end_date = '2026-09-30',
    updated_at = now()
WHERE promotion_id IN (3588, 3589, 3590);

-- This page is live but the displayed Juvéderm Day purchase date has passed;
-- retain it as an inactive historical record with corrected page evidence.
UPDATE public.clinic_promotions
SET campaign_start_date = '2026-08-19',
    campaign_end_date = '2026-08-19',
    promotion_content = jsonb_build_array(
      'Juvéderm Day 2026: buy one $75 gift card, get one free',
      'Mark your calendar for August 19 at 9am PT',
      'Receive an extra $75 off a treatment when redeeming Juvéderm gift cards within 30 days',
      'Total value up to $225 toward treatment at Enve California Aesthetics'
    ),
    is_active = false,
    is_offer_extracted = false,
    is_ocr_required = false,
    requires_cf_verification = false,
    updated_at = now()
WHERE promotion_id = 105;

-- A blocked or challenge page is not evidence that a promotion ended.
UPDATE public.clinic_promotions
SET requires_cf_verification = true,
    updated_at = now()
WHERE promotion_id IN (
  172,692,831,837,841,886,887,888,889,892,906,908,978,990,
  3409,3460,3493,3504,3539,3548,3586,3594
)
  AND NOT is_active;

DO $$
DECLARE
  active_reviewed integer;
  blocked_reviewed integer;
BEGIN
  SELECT count(*) INTO active_reviewed
  FROM public.clinic_promotions
  WHERE promotion_id IN (810,857,860,3588,3589,3590)
    AND is_active
    AND is_offer_extracted = false;
  SELECT count(*) INTO blocked_reviewed
  FROM public.clinic_promotions
  WHERE promotion_id IN (
    172,692,831,837,841,886,887,888,889,892,906,908,978,990,
    3409,3460,3493,3504,3539,3548,3586,3594
  )
    AND NOT is_active
    AND requires_cf_verification;
  IF active_reviewed <> 6 OR blocked_reviewed <> 22 THEN
    RAISE EXCEPTION 'promotion lifecycle review QA failed: active %, blocked %', active_reviewed, blocked_reviewed;
  END IF;
END $$;

COMMIT;
