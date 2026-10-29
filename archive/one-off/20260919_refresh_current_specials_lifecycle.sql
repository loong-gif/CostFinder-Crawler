BEGIN;

SELECT pg_advisory_xact_lock(hashtextextended('costfinder_current_specials_refresh_20260919', 0));

UPDATE public.clinic_promotions
SET campaign_start_date = '2026-09-01',
    campaign_end_date = '2026-09-30',
    is_offer_extracted = false,
    updated_at = now()
WHERE promotion_id = 2
  AND source_url = 'https://newlookskincenter.com/monthly-specials/';

UPDATE public.clinic_promotions
SET campaign_start_date = '2026-09-01',
    campaign_end_date = '2026-09-30',
    promotion_content = jsonb_build_array(
      'This month''s exclusive offers',
      'Emsella Chair — 50% off in September',
      'Exomind — 50% off in September',
      'Emsculpt Neo — 50% off in September'
    ),
    is_offer_extracted = false,
    updated_at = now()
WHERE promotion_id = 685
  AND source_url = 'https://longevityok.com/pages/specials-page';

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM public.clinic_promotions
    WHERE promotion_id IN (2,685)
      AND campaign_end_date < current_date
  ) THEN
    RAISE EXCEPTION 'current specials refresh left an expired active date';
  END IF;
END $$;

COMMIT;
