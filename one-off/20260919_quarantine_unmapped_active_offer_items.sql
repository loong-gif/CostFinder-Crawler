BEGIN;

SELECT pg_advisory_xact_lock(hashtextextended('costfinder_unmapped_active_offer_items_20260919', 0));

DO $$
DECLARE
  bad_count integer;
BEGIN
  SELECT count(*) INTO bad_count
  FROM public.promo_offer_master m
  JOIN public.promo_offer_items i ON i.offer_id = m.id
  LEFT JOIN public.clinic_services s ON s.service_id = i.service_id
  WHERE m.is_active
    AND (i.service_id IS NULL OR s.service_id IS NULL);
  IF bad_count <> 2 THEN
    RAISE EXCEPTION 'unmapped active offer precondition changed: expected 2, got %', bad_count;
  END IF;
END $$;

UPDATE public.promo_offer_master
SET is_active = false,
    updated_at = now()
WHERE id IN (10687,10688)
  AND is_active;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM public.promo_offer_master m
    JOIN public.promo_offer_items i ON i.offer_id = m.id
    LEFT JOIN public.clinic_services s ON s.service_id = i.service_id
    WHERE m.is_active
      AND (i.service_id IS NULL OR s.service_id IS NULL)
  ) THEN
    RAISE EXCEPTION 'unmapped active offer remains';
  END IF;
END $$;

COMMIT;
