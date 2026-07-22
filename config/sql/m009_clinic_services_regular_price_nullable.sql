-- clinic_services.regular_price is unit price and may be unknown until crawl fills it.
ALTER TABLE public.clinic_services
  ALTER COLUMN regular_price DROP NOT NULL;

COMMENT ON COLUMN public.clinic_services.regular_price IS
  'Unit price (e.g. USD per unit/syringe/area/vial); nullable until crawl extracts it.';
