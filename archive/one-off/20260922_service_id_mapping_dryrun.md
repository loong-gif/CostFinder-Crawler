# Service-id mapping repair dry run

Generated 2026-09-22 from the live `costfinder` Supabase project. No Supabase write was made.

## Proposed changes

- 147 brand-specific mappings lack direct service evidence across the item offer text and its linked promotion or membership source: 46 active items and 101 inactive items. The read-only query returns one row per item and proposes `service_id = NULL` for every row.
- 10 active, explicitly reviewed items have source-specific names but are attached to a generic/wrong service: `1653`, `5348`, `5350`, `2190`, `3529`, `3722`, `3726`, `4492`, `4493`, and `5197`. None has a unique matching `clinic_services` target in the same business, so each safe proposal is also `service_id = NULL`.
- The second group spans 9 offers because items `1653` and `5348` belong to offer `1615`.

## Important operational effect

The current `trg_deactivate_offer_with_unresolved_item` trigger deactivates an active offer when an item becomes unresolved. Applying the active rows as proposed would deactivate 54 distinct active offers: 45 from the unsupported-brand group and 9 from the explicit wrong-service group. This dry run intentionally does not alter that trigger or activate/deactivate anything.

## Verification

- Cross-clinic mappings: 0.
- Repository-required active blanket-offer check (based on `offer_raw_text`): 0 rows at this snapshot. That check does not cover broad wording stored only in linked membership or promotion content, which is why the first query retains its wider evidence review.

Run [20260922_service_id_mapping_dryrun.sql](20260922_service_id_mapping_dryrun.sql) to reproduce the row-level output. It contains only `SELECT` statements.
