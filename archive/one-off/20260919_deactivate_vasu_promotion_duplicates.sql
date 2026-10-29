BEGIN;

DO $$
DECLARE
  target_count integer;
BEGIN
  SELECT count(*)
  INTO target_count
  FROM public.promo_offer_master
  WHERE id IN (10521, 10522)
    AND business_id = 1194
    AND promotion_id = 84
    AND membership_plan_id IS NULL
    AND is_active;

  IF target_count <> 2 THEN
    RAISE EXCEPTION
      'Vasu promotion duplicate precondition failed: expected 2 active rows, got %',
      target_count;
  END IF;
END $$;

UPDATE public.promo_offer_master
SET is_active = false,
    updated_at = now()
WHERE id IN (10521, 10522)
  AND business_id = 1194
  AND promotion_id = 84
  AND membership_plan_id IS NULL
  AND is_active;

COMMIT;
