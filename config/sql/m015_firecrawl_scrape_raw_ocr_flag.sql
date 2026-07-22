BEGIN;

ALTER TABLE public.firecrawl_scrape_raw
  ADD COLUMN IF NOT EXISTS is_ocr_required boolean NOT NULL DEFAULT false;

COMMIT;
