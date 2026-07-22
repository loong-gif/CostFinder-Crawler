BEGIN;

ALTER TABLE public.firecrawl_scrape_raw
  ADD COLUMN IF NOT EXISTS markdown text,
  ADD COLUMN IF NOT EXISTS html text,
  ADD COLUMN IF NOT EXISTS raw_html text,
  ADD COLUMN IF NOT EXISTS links jsonb,
  ADD COLUMN IF NOT EXISTS metadata jsonb,
  ADD COLUMN IF NOT EXISTS screenshot text,
  ADD COLUMN IF NOT EXISTS warning text,
  ADD COLUMN IF NOT EXISTS scrape_job_id text,
  ADD COLUMN IF NOT EXISTS credits_used integer;

UPDATE public.firecrawl_scrape_raw
SET
  markdown = COALESCE(response_json->'data'->>'markdown', response_json->>'markdown'),
  html = COALESCE(response_json->'data'->>'html', response_json->>'html'),
  raw_html = COALESCE(response_json->'data'->>'rawHtml', response_json->>'rawHtml'),
  links = COALESCE(response_json->'data'->'links', response_json->'links'),
  metadata = COALESCE(response_json->'data'->'metadata', response_json->'metadata'),
  screenshot = COALESCE(response_json->'data'->>'screenshot', response_json->>'screenshot'),
  warning = COALESCE(response_json->'data'->>'warning', response_json->>'warning', response_json->>'warning'),
  scrape_job_id = response_json->>'id',
  credits_used = NULLIF(response_json->>'creditsUsed', '')::integer
WHERE response_json IS NOT NULL;

ALTER TABLE public.firecrawl_scrape_raw DROP COLUMN IF EXISTS response_json;

COMMIT;
