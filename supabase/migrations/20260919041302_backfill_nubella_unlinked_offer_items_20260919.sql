SELECT setval(
  pg_get_serial_sequence('public.clinic_services', 'service_id'),
  (SELECT COALESCE(max(service_id), 1) FROM public.clinic_services)
);

DO $$
DECLARE
  v_source_url text := 'https://nubellamedspa.com/nu-bella-store';
  v_microneedling_id bigint;
  v_morpheus8_id bigint;
  v_kybella_id bigint;
  v_nad_id bigint;
BEGIN
  SELECT service_id INTO v_microneedling_id
  FROM public.clinic_services
  WHERE business_id = 2857 AND service_name = 'Microneedling'::public.clinic_service_name_enum
  ORDER BY service_id LIMIT 1;
  IF v_microneedling_id IS NULL THEN
    INSERT INTO public.clinic_services (business_id, service_name, source_url)
    VALUES (2857, 'Microneedling', v_source_url)
    RETURNING service_id INTO v_microneedling_id;
  END IF;

  SELECT service_id INTO v_morpheus8_id
  FROM public.clinic_services
  WHERE business_id = 2857 AND service_name = 'Morpheus8'::public.clinic_service_name_enum
  ORDER BY service_id LIMIT 1;
  IF v_morpheus8_id IS NULL THEN
    INSERT INTO public.clinic_services (business_id, service_name, source_url)
    VALUES (2857, 'Morpheus8', v_source_url)
    RETURNING service_id INTO v_morpheus8_id;
  END IF;

  SELECT service_id INTO v_kybella_id
  FROM public.clinic_services
  WHERE business_id = 2857 AND service_name = 'Kybella'::public.clinic_service_name_enum
  ORDER BY service_id LIMIT 1;
  IF v_kybella_id IS NULL THEN
    INSERT INTO public.clinic_services (business_id, service_name, source_url)
    VALUES (2857, 'Kybella', v_source_url)
    RETURNING service_id INTO v_kybella_id;
  END IF;

  SELECT service_id INTO v_nad_id
  FROM public.clinic_services
  WHERE business_id = 2857 AND service_name = 'NAD Therapy'::public.clinic_service_name_enum
  ORDER BY service_id LIMIT 1;
  IF v_nad_id IS NULL THEN
    INSERT INTO public.clinic_services (business_id, service_name, source_url)
    VALUES (2857, 'NAD Therapy', v_source_url)
    RETURNING service_id INTO v_nad_id;
  END IF;

  UPDATE public.promo_offer_items
  SET service_id = v_microneedling_id, updated_at = now()
  WHERE offer_item_id IN (5379, 5380, 5381)
    AND service_id IS NULL;

  UPDATE public.promo_offer_items
  SET service_id = v_morpheus8_id, updated_at = now()
  WHERE offer_item_id = 5383
    AND service_id IS NULL;

  UPDATE public.promo_offer_items
  SET service_id = v_kybella_id, updated_at = now()
  WHERE offer_item_id = 5385
    AND service_id IS NULL;

  UPDATE public.promo_offer_items
  SET service_id = v_nad_id, updated_at = now()
  WHERE offer_item_id = 5386
    AND service_id IS NULL;
END $$;
