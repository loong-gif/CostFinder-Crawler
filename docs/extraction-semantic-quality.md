# Extraction semantic quality (promotion_content & benefits)

Hard rules for **acceptance-ready** `clinic_promotions.promotion_content` and `clinic_memberships.benefits`. Structural JSON validity is not enough: post-LLM gates in `utils/extraction_semantic_quality.py` and `validate_*` in `utils/recent_raw_extraction.py` must pass before persist.

## promotion_content

Each array element should be **one bound offer card**: service or package name, promo price or explicit discount, and conditions together (same segment, optionally joined with ` | `). Do not store membership monthly fees, Beauty Bank tiers, or member-only price lists as promotions.

| Issue type | Meaning | Gate (extract) |
| --- | --- | --- |
| `FRAGMENTED_UNBOUND_PRICES` | Orphan `$` lines or prices split from service names | Fail |
| `MARKDOWN_OR_SHOPIFY_RESIDUE` | `![…]()`, markdown links, Shopify Regular/Sale UI | Fail |
| `SOFT_CTA_ONLY` | Terms / “mention this page” without numeric offer | Fail |
| `MEMBERSHIP_AS_PROMO` | Membership dues or tier blocks in promo content | Fail |
| `UI_POLLUTION` | Contact us, empty cart, `$$ImageUrl$$` | Warn (audit); extract fails on high-severity subset via binding/quality |
| `TITLE_LOOKS_LIKE_SLUG` | Title like `monthly-promos-page` | Warn (audit) |

Page-level routing (PR #7): `utils/promotion_source_page.py` rejects membership/blog/pricing/catalog pages before LLM write. Card binding: `utils/promotion_content_cards.py`.

## benefits

One `clinic_memberships` row per priced tier on the source page. `benefits` holds **non-SKU perks** as deduplicated verbatim bullets.

| Issue type | Meaning | Gate (extract) |
| --- | --- | --- |
| `NULL_BENEFITS_PRICED` | Price present, benefits empty | Fail |
| `INCOMPLETE_BENEFITS_DISCOUNT_ONLY` | Only a thin discount line (e.g. “10% off skincare”) | Fail |
| `DUPLICATE_BENEFIT_BULLET` | Exact duplicate strings in one array | Warn (audit); dedupe at extract when possible |
| `MARKDOWN_OR_SHOPIFY_RESIDUE` | Links / markdown in benefits | Fail |
| `NEEDS_OCR` | `is_ocr_required` and still empty benefits | Fail when flag passed into `validate_membership` |

## Where it runs

- **Extract-time**: `validate_promotion`, `validate_membership` → `GateDecision.reason` (lowercase issue id).
- **Read-only audit**: `utils/extraction_quality_audit.py` → uppercase `issue_type` aligned with sheet QA reports.
- **Prompts**: `schema/promotion_extraction_schema.json`, `schema/membership_extraction_schema.json` field descriptions.

See also pipeline layering in `docs/data-model-pipeline.md` and architecture skill `.cursor/skills/costfinder-architecture/SKILL.md`.
