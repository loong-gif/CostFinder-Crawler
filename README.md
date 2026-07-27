# CostFinder

CostFinder collects publicly available clinic service and promotion evidence, then stores normalized records in the current Supabase schema.

## Active data model

The authoritative schema snapshot is [`schema/live_schema_snapshot.json`](schema/live_schema_snapshot.json). Current production paths use:

- `master_business_info` for clinic identity and website
- `clinic_services` for normal service pricing
- `clinic_memberships` for membership programs
- `clinic_promotions`, `promo_offer_master`, and `promo_offer_items` for promotion evidence and offers
- `firecrawl_search_raw` and `firecrawl_scrape_raw` for immutable search/scrape evidence

The application intentionally contains no runtime path to the retired staging, monitor-state, or social-staging tables.

## Setup

```bash
uv venv .venv --python 3.11
source .venv/bin/activate
uv pip install -r requirements.txt
```

Configure `SUPABASE_URL` plus an appropriate server-side credential, along with `FIRECRAWL_API_KEY` and the LLM credentials required by the command you run.

## Active commands

```bash
# Discover and persist normal service records from Firecrawl evidence
python scripts/run_clinic_services_firecrawl.py --business_id 1001 --dry_run

# Search, scrape, extract, and persist a single business domain
python scripts/run_domain_architecture_pipeline.py --domain example.com --apply

# Populate normal-service rows from current offer records
python scripts/backfill_clinic_services_from_offers.py --dry-run
```

Run the test suite with `python -m pytest`.

## Repository conventions

- `schema/live_schema_snapshot.json` is a checked-in contract of the live database.
- Historical migration material is retained only where it remains compatible with the live schema; Git history contains retired schema designs.
- `scripts/one_off/` contains isolated operational utilities and is not a production entrypoint.
