# Live data model and pipeline

This document describes CostFinder's active Supabase data model and the production ingestion path that populates it. It was verified against the live `costfinder` project on 2026-07-30.

The live database is the source of truth for field names and foreign keys. [`schema/live_schema_snapshot.json`](../schema/live_schema_snapshot.json) is the checked-in repository contract, but it can lag a live schema change; refresh it alongside future database changes rather than using it to override verified live facts. Historical schema material under `config/sql/archive/` is context only and must not be executed as the current schema.

## Core relationships

```mermaid
erDiagram
  master_business_info ||--o{ clinic_services : business_id
  master_business_info ||--o{ clinic_memberships : business_id
  master_business_info ||--o{ clinic_promotions : business_id
  master_business_info ||--o{ promo_offer_master : business_id
  master_business_info ||--o{ business_reviews : business_id
  master_business_info ||--o{ claims : business_id

  clinic_memberships ||--o{ promo_offer_master : membership_plan_id
  clinic_promotions ||--o{ promo_offer_master : promotion_id
  promo_offer_master ||--o{ promo_offer_items : offer_id
  clinic_services ||--o{ promo_offer_items : service_id
  promo_offer_master ||--o{ promo_offer_quality_reviews : offer_id
  promo_offer_master ||--o{ claims : deal_id
  profiles ||--o{ claims : consumer_id

  firecrawl_search_raw ||--o{ firecrawl_scrape_raw : search_raw_id
```

`master_business_info` is the business root. An offer's commercial terms belong to `promo_offer_master`; its service composition belongs to one or more `promo_offer_items` rows. An item may have a null `service_id` until the application can resolve a reliable, same-business catalogue match.

## Public schema

### Business and normalized catalogue

| Table | Purpose | Important fields |
| --- | --- | --- |
| `master_business_info` | Clinic identity and discovery seed. | `business_id`, name, address, city, category, website, social URLs, Google place ID, logo, review metadata. |
| `clinic_services` | Normal, non-promotional service catalogue. | `service_id`, `business_id`, `service_name`, `regular_price`, `unit_type`, `service_category`, `service_area`, `source_url`, `service_raw_text`. |
| `clinic_memberships` | Purchasable membership plans and their benefits. | `plan_id`, `business_id`, `membership_name`, `membership_price`, `billing_period`, `minimum_commitment_months`, `benefits`, `source_url`. |

`clinic_services.regular_price` is a public catalogue price for the stated `unit_type`; it is nullable when a service is known but a reliable public price has not been extracted. It is not a promotional, package, or market-average price. `clinic_memberships.membership_price` is the recurring or one-time amount described by `billing_period`; `benefits` is a JSON array of plan entitlements.

### Promotions and offers

| Table | Purpose | Important fields |
| --- | --- | --- |
| `clinic_promotions` | Campaign-level source page and lifecycle. | `promotion_id`, `business_id`, `promotion_title`, `source_url`, campaign dates, `is_active`, `promotion_content`. |
| `promo_offer_master` | Offer-level price, eligibility, provenance, and activity state. | `id`, `business_id`, `promotion_id`, `membership_plan_id`, regular and discount price fields, `price_model`, membership/new-customer requirements, `conditions` (`text[]` of extra requirement strings), `offer_fingerprint`, `offer_raw_text`, `is_active`. |
| `promo_offer_items` | The service components of an offer, including a single-service offer. | `offer_item_id`, `offer_id`, `item_name`, `service_id`, `delivered_units`, `unit_price`. |

`promo_offer_master` describes the commercial offer, not a specific service. Its `price_model` distinguishes total-price, per-unit, and starting-price offers. `regular_price` and `discount_price` are the prices stated by the promotion; `discount_amount` and `discount_percent` are comparative fields when supported by evidence. `promo_offer_items` supplies the service relationship and stated delivered quantity. A package can therefore have multiple items, while a single offer still has one item. `delivered_units` is recorded only when a positive delivered quantity is explicitly stated; `unit_price` records an item-level price when available.

`conditions` is nullable `text[]`: `NULL` means not processed or under review, an empty array means the matched source has no additional restriction, and non-empty values contain only validated requirement text. Membership and new-customer eligibility remain in their dedicated boolean columns and are not duplicated here.

## Normalized-write gates

The following controls keep discovery output from becoming catalogue or offer data prematurely:

- A candidate marked `review` is never written automatically. A `ready` candidate still needs route classification for `/product(s)/` pages and same-page specificity resolution: a supported specific service suppresses its generic parent, except when a true category page independently establishes multiple services.
- `clinic_services.unit_type` is a source-backed unit semantic, not a completeness placeholder. Keep it `NULL` when unknown; do not default it to `treatment` or another catch-all value.
- Scraped or DOM text alone cannot support a Promotion, Membership benefit, or Offer-condition write. Use the rendered-visibility gate in [`rendered_visibility_qa.md`](rendered_visibility_qa.md); `dom_only_hidden`, `page_unavailable`, and pending image review remain non-writing states.
- `promo_offer_items.item_name` preserves the source's commercial line item; `service_id` is a separate, same-business catalogue association. Never replace the item name with an inferred service enum. A blanket offer (for example, all services, products, or gift cards) remains one offer with `service_id = NULL`, so it stays visible on clinic/city pages without appearing on a treatment page.
- Writers are idempotent: blanket wording collapses to one unlinked item, an identical or inactive-duplicate offer is a no-op, and a supported change updates the existing active row. Shared-price lines stay one package offer with item prices `NULL`; a known membership requirement sets `membership_plan_id` only.
- Database-derived discount amount/percent values matching the two source-stated prices do not trigger a rewrite when extraction leaves those comparative fields empty. They remain structural arithmetic rather than extracted source facts.
- Bundle offers pass the same evidence validator as domain extraction, using the matching scrape text. An extraction payload alone cannot support its own commercial fields.

### Evidence and quality control

| Table | Purpose | Important fields |
| --- | --- | --- |
| `firecrawl_search_raw` | Recoverable result of each search request. | `id`, `request_fingerprint`, `search_query`, `response_json`, success and error fields. |
| `firecrawl_scrape_raw` | Recoverable scrape payload linked to its search request. | `id`, `request_fingerprint`, `source_url`, `search_raw_id`, Markdown/HTML payloads, links, metadata, OCR fields, success and error fields. |
| `promo_offer_quality_reviews` | Review queue and resolution record for offer-quality findings. | `review_id`, `offer_id`, rule name, source snapshot, proposed patch, status, resolution fields. |

The raw Firecrawl tables are the evidence layer, not an alternate catalogue. A normalized record must retain its source URL and be traceable back to the captured search or scrape material when that evidence was used. `promo_offer_quality_reviews` stores audit outcomes; it does not replace the offer itself.

The `extract-pages` planner independently classifies each captured page before routing it to promotions or memberships. Promotion content must retain complete source offer cards and their linked terms. Membership benefits are compared with a separate source entitlement inventory. Tesseract CLI must be installed on scraping and render-review hosts. Relevant page images are OCRed during scraping, and images visible in the browser render are OCRed again for the visibility manifest. Missing, unreadable, or unverified image text keeps membership benefits in review; extraction plans retain the scrape ID, fingerprint, timestamp, and OCR evidence metadata used for each decision.

### Consumer workflow

| Table | Purpose | Important fields |
| --- | --- | --- |
| `profiles` | Consumer profile associated with `auth.users`. | `id`, contact and verification fields, location, alert preferences, status. |
| `claims` | A consumer's interest in an offer and the clinic follow-up lifecycle. | `id`, `consumer_id`, `deal_id`, `business_id`, status, preferred and booked times, expiry and relay fields. |
| `business_reviews` | Scraped Google Maps review evidence for a clinic. | `id`, `business_id`, Google review ID, rating, text, media, publication time. |

Consumer claims and review ingestion are adjacent product workflows. They are not outputs of the Firecrawl price-extraction pipeline.

## Evidence-to-record pipeline

```text
master_business_info.website
  -> domain and service-page discovery
  -> firecrawl_search_raw
  -> candidate URL, domain, and price-signal gates
  -> firecrawl_scrape_raw
  -> validated extraction and normalization
  -> clinic_services / clinic_memberships / clinic_promotions
  -> promo_offer_master + promo_offer_items
  -> promo_offer_quality_reviews when a quality rule requires review
```

[`scripts/run_domain_architecture_pipeline.py`](../scripts/run_domain_architecture_pipeline.py) is the domain-level path for search, scrape, extraction, and persistence. [`scripts/run_clinic_services_firecrawl.py`](../scripts/run_clinic_services_firecrawl.py) is the service-catalogue discovery path. Both preserve search and scrape evidence independently of normalized writes.

Catalogue-price writes are guarded by [`utils/service_price_guard.py`](../utils/service_price_guard.py): promotional and blog sources, unsupported market ranges, unnormalised package totals, and mismatched sub-treatments must not overwrite `clinic_services.regular_price`. Service-to-offer matching is deterministic within the same `business_id`; uncertain matches remain unlinked rather than creating a cross-business association.

## SAG projection

`sag` is a rebuildable, server-only projection for the Structured Agentic Graph (SAG) pilot. It is not a source-of-truth business schema and must not be used to update public catalogue or promotion tables.

The pilot reads structured service, membership, and promotion records, then creates versioned `sag.document` rows. Documents are split into `sag.chunk` rows, enriched as `sag.event` and `sag.entity` records linked through `sag.event_entity`, and indexed through the corresponding chunk, event, and entity embedding tables. `sag.index_version` identifies each corpus build, while `sag.index_job` tracks asynchronous upsert or delete work.

The current project accesses SAG through server-side database connectivity used by [`scripts/sag_persistent_pilot.py`](../scripts/sag_persistent_pilot.py). It is intentionally outside client access: as verified on 2026-07-30, the `anon`, `authenticated`, and `public` roles have no `USAGE` on the `sag` schema and no `SELECT` access to its tables. RLS is not currently enabled on these private tables, so documentation must not describe SAG as RLS-protected.
## Firecrawl Monitor sync

Monitor-specific exceptions (NULL service/date, `is_offer_extracted=false` while mapping is incomplete) are defined in [`AGENTS.md`](../AGENTS.md) and [`monitor_price_sync.md`](monitor_price_sync.md).
