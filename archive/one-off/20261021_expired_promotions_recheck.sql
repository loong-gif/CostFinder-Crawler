BEGIN;

SELECT pg_advisory_xact_lock(hashtextextended('costfinder_expired_promotions_recheck_20261021', 0));

DO $$
DECLARE
  expected integer := 12;
  actual integer;
BEGIN
  SELECT count(*) INTO actual
  FROM public.clinic_promotions
  WHERE promotion_id IN (64, 68, 685, 860, 921, 3416, 3495, 3498, 3500, 3544, 3550, 3551)
    AND is_active
    AND campaign_end_date < current_date;
  IF actual <> expected THEN
    RAISE EXCEPTION 'expected % active expired promotions, found %', expected, actual;
  END IF;
END $$;

UPDATE public.clinic_promotions
SET is_active = false,
    updated_at = now()
WHERE promotion_id IN (64, 68, 685, 860, 921, 3416, 3495, 3498, 3500, 3544, 3550, 3551)
  AND is_active
  AND campaign_end_date < current_date;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM public.clinic_promotions
    WHERE promotion_id IN (64, 68, 685, 860, 921, 3416, 3495, 3498, 3500, 3544, 3550, 3551)
      AND is_active
  ) THEN
    RAISE EXCEPTION 'target promotions still active after retire';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM public.promo_offer_master m
    JOIN public.clinic_promotions p ON p.promotion_id = m.promotion_id
    WHERE p.promotion_id IN (64, 68, 685, 860, 921, 3416, 3495, 3498, 3500, 3544, 3550, 3551)
      AND m.is_active
  ) THEN
    RAISE EXCEPTION 'active offers remain under retired promotions';
  END IF;
END $$;

COMMIT;
