---
name: costfinder-offer-quality
description: Acceptance bar for clinic_promotions.promotion_content and clinic_memberships.benefits — gates, issue types, and schema intent.
---

# Offer & membership content quality

Read **[docs/extraction-semantic-quality.md](../../../docs/extraction-semantic-quality.md)** before changing extract gates, audit issue types, or LLM schema descriptions for promotions/memberships.

Implementation map:

- Post-LLM gates: `utils/extraction_semantic_quality.py`
- Promotion validate: `utils/recent_raw_extraction.validate_promotion` (+ `promotion_content_cards`, `promotion_source_page`)
- Membership validate: `utils/recent_raw_extraction.validate_membership`
- Table audit: `utils/extraction_quality_audit.audit_promotions` / `audit_memberships`

Do not weaken PR #7 page-type or card-binding checks when editing semantic rules. In `extract-pages` the same promotion gates run per card through `validate_promotion_card`, and card binding comes from pairing with the source review.
