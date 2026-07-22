BEGIN;

CREATE TABLE IF NOT EXISTS public.firecrawl_scrape_raw (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  request_fingerprint text NOT NULL,
  source_url text NOT NULL,
  search_raw_id bigint NULL
    REFERENCES public.firecrawl_search_raw(id) ON DELETE SET NULL,
  markdown text,
  html text,
  raw_html text,
  links jsonb,
  metadata jsonb,
  screenshot text,
  warning text,
  scrape_job_id text,
  credits_used integer,
  success boolean NOT NULL DEFAULT true,
  error_message text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT firecrawl_scrape_raw_fingerprint_key UNIQUE (request_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_firecrawl_scrape_raw_source_url
  ON public.firecrawl_scrape_raw (source_url, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_firecrawl_scrape_raw_search_raw_id
  ON public.firecrawl_scrape_raw (search_raw_id)
  WHERE search_raw_id IS NOT NULL;

ALTER TABLE public.firecrawl_scrape_raw ENABLE ROW LEVEL SECURITY;

COMMIT;
