# Standard extraction pipeline (decisions)

Layering (do not mix):

1. **Raw** — Firecrawl search/scrape tables unchanged.
2. **LLM** — JSON schema in `schema/*_extraction_schema.json`; `explanation` first for audit only.
3. **Deterministic gates** — `validate_service` / `validate_membership` / `validate_promotion` in `utils/recent_raw_extraction.py`, plus promotion page/card helpers and **semantic** rules in `utils/extraction_semantic_quality.py`. Failed gates return `GateDecision(accepted=False, reason=…)`; no silent PASS.
4. **Persist** — `utils/extraction_persist.py` and domain scripts only after gates pass.
5. **Read-only QA** — `utils/extraction_quality_audit.py` for uploaded tables (issue types mirror extract gates).

Semantic acceptance criteria for `promotion_content` and `benefits`: **[extraction-semantic-quality.md](extraction-semantic-quality.md)**.
