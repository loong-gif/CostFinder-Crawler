alter table public.firecrawl_scrape_raw
  add column if not exists response_json jsonb;

comment on column public.firecrawl_scrape_raw.response_json is
  'Complete Firecrawl Scrape API response envelope, retained as immutable raw evidence.';
