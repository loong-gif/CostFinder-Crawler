# Promotion data quality

When updating `promo_offer_master` or `promo_offer_items`, blanket offers must not be linked to a specific service. Before completing each data-update batch, run the following check. It must return zero rows, unless a row is deliberately retained and documented in the batch result.

```sql
select m.id, m.offer_raw_text, i.item_name
from promo_offer_master m
join promo_offer_items i on i.offer_id = m.id
where m.is_active
  and i.service_id is not null
  and m.offer_raw_text ~* '(all services|any service|all products|all treatments|gift card|skin ?care products)';
```

For any matching row that is not intentionally retained, set `promo_offer_items.service_id` to `NULL`; do not change the offer text solely to make this check pass.

## Monitor synchronization policy

For Firecrawl promotion synchronization, a missing year or unresolved service mapping is not a write blocker. Store unknown campaign dates and service IDs as NULL; retain evidenced item names, prices, quantities and conditions. Preserve a known valid service mapping and existing Offer/Item IDs when the match is unique. Keep `is_offer_extracted=false` while service mapping remains incomplete. Reuse persisted Ego Lite verification for the capture it actually reviewed, keeping its original timestamp; later captures require current visibility evidence. Source/Offer identity ambiguity, contradictory price evidence and manual protections still require review.

## Standard pipeline

All data work runs through `python scripts/promo_pipeline.py <command>`. Python never connects to or writes the production database and `DB_URL` is not used: every database read and write goes through the Supabase MCP (`execute_sql`). Write commands have three steps: `read-sql` (run via MCP, save the `snapshot` value as `snapshot.json`), `plan --snapshot snapshot.json`, `apply-sql --plan plan.json` (run the resulting single `DO` block via MCP). Direct write paths raise (`utils/pipeline_run.py`). Monitor-host jobs only write SQL to `state/pending_sql/`; review and execute those files via MCP. Do not write one-off scripts: new logic goes into a `utils/` module plus a registered command (`tests/test_no_one_off_scripts.py` enforces this). Old one-off scripts live in `archive/one-off/` and are not run. Code that runs on the monitor host or any remote machine must come from this repository.

Stage order: `discover-businesses` → `register-businesses` → `discover-pages` → `review-pages` → `render-shards` → render review → `render-verdicts` → `finalize-pages` → `crawl` → `extract-pages` → `extract-offers` → `link-services`. Maintenance: `recrawl-sources`, `visibility-manifest`, `read-snapshot`, `merge-promotions`, `enforce-promotion-rules`, `delete-offer-shells`, `deactivate-expired`, `monitor`, `quality`, `batch-gate`.

Hard rules enforced in code and in the database (`supabase/migrations/20261103043251_promotion_identity_guards.sql`):
- One `clinic_promotions` row per `(business_id, fn_source_url_key(source_url))`; one `clinic_memberships` row per `(business_id, fn_source_url_key(source_url), lower(btrim(membership_name)))`.
- A page under a promotion hub path (`/specials/...`, `/promotions/...`) is written as the shallowest hub URL.
- `promotion_title` is the source page `<title>` taken by code from the crawl result, never written by an LLM.
- An active promotion whose URL path has no promotion keyword needs explicit discount evidence; a plain list or package price is not a promotion.
- Promotions past `campaign_end_date` are not written and are deactivated daily.
- Writes are upserts by identity key; never clear and rewrite a table. Snapshots are verified table by table against `count(*)`, and every `apply.sql` checks that the planned rows have not changed before writing.

Database changes may add triggers, functions, CHECK constraints and indexes to enforce rules; never add tables or columns.

## Rules and what enforces them

| Rule | Enforced by |
|---|---|
| Run the test suite before handing off | `.venv/bin/python3 -m pytest tests` |
| `promotion_content` is verbatim source text; a struck original price stays `~~$135~~` | strikethrough cases in `tests/test_page_extraction.py` |
| Image OCR runs only through `utils.paddle_ocr_markdown.ocr_lines` | `tests/test_image_ocr.py::test_image_ocr_has_a_single_entry_point` |
| An LLM answer that fails validation rejects its page, never the whole plan, and stays cached as returned | `tests/test_page_extraction.py::test_paraphrased_source_review_rejects_only_its_page_and_is_not_requested_again` |
