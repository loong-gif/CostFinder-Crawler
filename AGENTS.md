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

For Firecrawl promotion synchronization, a missing year or unresolved service mapping is not a write blocker. A date without a year means the observed year; store unreadable campaign dates and unknown service IDs as NULL; retain evidenced item names, prices, quantities and conditions. Preserve a known valid service mapping and existing Offer/Item IDs when the match is unique. `is_offer_extracted` is derived by the database trigger `trg_sync_is_offer_extracted` on `promo_offer_master` (true when a row with the matching `promotion_id` / `membership_plan_id` exists, false otherwise); service mapping completeness does not affect it. Reuse persisted Ego Lite verification for the capture it actually reviewed, keeping its original timestamp; later captures require current visibility evidence. Everything evidence can settle is handled by code (`docs/monitor_price_sync.md`): a uniquely matched inactive Offer or Promotion is reactivated, identical duplicate Offers keep the lowest ID, contradictory amounts keep the page's stated sale price and drop the conflicting derived field, and blocked or incomplete captures keep the live rows and are recaptured. Only Source/Offer identity that evidence cannot settle, a cross-domain redirect to a third party and manual protections require review.

## Standard pipeline

All data work runs through `python scripts/promo_pipeline.py <command>`. Python never writes the production database and `DB_URL` is not used. Reads run automatically: `read-snapshot <read.sql>` executes the read-only snapshot query with the Supabase CLI (`--linked`) and writes `snapshot.json`; reading through Python database clients is replaced by `read-sql` plus `read-snapshot`. Writes go through the Supabase MCP (`execute_sql`). Write commands have three steps: `read-sql` (then `read-snapshot`), `plan --snapshot snapshot.json`, `apply-sql --plan plan.json` (run the resulting single `DO` block via MCP). Direct write paths raise (`utils/pipeline_run.py`). Monitor-host jobs only write SQL to `state/pending_sql/`; `pending-sql --pull` copies those files back, execute each via MCP, then `pending-sql --mark-applied FILE` moves it into `applied/` locally and on the monitor host. Do not write one-off scripts: new logic goes into a `utils/` module plus a registered command (`tests/test_no_one_off_scripts.py` enforces this). Old one-off scripts live in `archive/one-off/` and are not run. Code that runs on the monitor host or any remote machine must come from this repository.

Stage order: `discover-businesses` → `register-businesses` → `discover-pages` → `review-pages` → `render-pages` → `crawl` → `extract-pages` → `extract-offers` → `link-services`. Maintenance: `recrawl-sources`, `visibility-manifest`, `read-snapshot`, `merge-promotions`, `enforce-promotion-rules`, `delete-offer-shells`, `deactivate-expired`, `monitor`, `quality`, `batch-gate`. `discover-pages`, `review-pages` and `render-pages` share one batch directory (`--batch-dir`, default `state/discovery`); every file in it is located by `utils/discovery_batch.py`, and `crawl` / `extract-pages` read its `decisions.json` by default.

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
| `promotion_content` and membership `benefits` must be traceable to the source page: every amount and percentage appears on the page and the wording can be located there; rephrasing is allowed. A struck original price keeps its `~~$135~~` marker | `utils.promo_extraction.traceable` and `utils.render_review.claim_matches` at write time; tests in `tests/test_page_extraction_promotions.py`, `tests/test_page_extraction_memberships.py` and `tests/test_visibility_manifest.py` |
| A membership is written only when its plan name and "name $fee" are visible in the ego-lite render; benefits are null when the page states none (never placeholders or inferences) | `tests/test_page_extraction_memberships.py::test_membership_name_or_fee_not_visible_in_the_render_is_rejected` |
| A page discovered as a membership page (`kind: membership`) writes only `clinic_memberships`, never `clinic_promotions`, even when its source review is `mixed` | `tests/test_page_extraction_memberships.py::test_membership_page_with_promotion_cards_writes_only_memberships` |
| Stored promotion content carries no links, image markup or URLs | `tests/test_page_extraction_source_review.py::test_link_residue_is_not_stored_as_promotion_content` |
| Data QA of promotions, memberships and offers verifies each claim in an ego-lite render as described in `docs/rendered_visibility_qa.md` (`visibility-manifest --plan`); crawled Markdown and LLM judgement are not visibility evidence | `docs/rendered_visibility_qa.md`; this table |
| `is_offer_extracted` equals "an offer row exists for the promotion / membership" | Trigger `trg_sync_is_offer_extracted` (`supabase/migrations/20261106000000_sync_is_offer_extracted.sql`) |
| An offer is written only when its text is a complete statement about a service or product: flattened table fragments (unbalanced parentheses, a `\|` part starting with and/or/including/excluding), membership prepay terms, gift card purchases and discounts on events are rejected | `utils.offer_scope_filter.non_promotable_reason` called from `utils.offer_stage_extract.candidates_from_payload`; tests in `tests/test_offer_extraction_fixes.py` |
| Image OCR runs only through `utils.paddle_ocr_markdown.ocr_lines` | `tests/test_image_ocr.py::test_image_ocr_has_a_single_entry_point` |
| An LLM answer that fails validation rejects its page, never the whole plan, and stays cached as returned | `tests/test_page_extraction_source_review.py::test_paraphrased_source_review_rejects_only_its_page_and_is_not_requested_again` |

## Working in this repository

- When the pre-commit hook rejects a commit for file size (over 20,000 bytes, about 5,000 tokens), split the named file by feature according to the error message, then commit again.
- Before writing a new function or rule, search the project for an existing one and reuse it.
