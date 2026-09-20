"""Resolve membership offer scopes into concrete clinic service items."""
from __future__ import annotations

import re
from typing import Any, Iterable

PRODUCT_RE = re.compile(r"\b(?:all\s+)?(?:products?|skincare|skin\s*care|retail)\b", re.I)
ALL_SERVICE_RE = re.compile(r"\ball\s+(?:services?|treatments?)\b", re.I)


def classify_membership_offer_scope(text: str) -> str:
    value = str(text or "").strip()
    if PRODUCT_RE.search(value):
        return "product"
    if ALL_SERVICE_RE.search(value):
        return "all_services"
    return "service"


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.casefold()) if len(token) > 2}


def expand_membership_offer_services(
    offer_text: str,
    services: Iterable[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Return (scope, concrete services, review reason).

    Matching is deliberately deterministic: exact normalized names first, then
    a unique token containment match. Broad service scopes expand to the
    current business catalogue snapshot.
    """
    rows = [row for row in services if row.get("service_id") is not None]
    scope = classify_membership_offer_scope(offer_text)
    if scope == "product":
        return scope, [], "product_offer_without_product_catalog"
    if scope == "all_services":
        return scope, rows, None if rows else "business_has_no_services"

    text = str(offer_text or "").strip()
    text_cf = text.casefold()
    # The extractor's canonical vocabulary resolves common shorthand such as
    # "filler" and "microneedling" more reliably than raw token overlap.
    from utils.offer_extraction_llm import canonicalize_service_name
    canonical = canonicalize_service_name(text)
    canonical_matches = [
        row for row in rows
        if str(row.get("service_name") or "").strip().casefold() == str(canonical or "").strip().casefold()
    ]
    if len(canonical_matches) == 1 and canonical not in {"Others", "Neurotoxin"}:
        return scope, canonical_matches, None
    category_match = re.search(r"\ball\s+([a-z][a-z -]+?)(?:\s+(?:services?|treatments?))?(?:\b|$)", text_cf)
    if category_match:
        category_words = _tokens(category_match.group(1))
        category_rows = [row for row in rows if category_words & _tokens(str(row.get("service_category") or ""))]
        if category_rows:
            return scope, category_rows, None
    exact = [row for row in rows if str(row.get("service_name") or "").strip().casefold() in text_cf]
    if len(exact) == 1:
        return scope, exact, None
    text_tokens = _tokens(text)
    candidates = [row for row in rows if _tokens(str(row.get("service_name") or "")) & text_tokens]
    if len(candidates) == 1:
        return scope, candidates, None
    return scope, [], "service_mapping_ambiguous_or_missing"
