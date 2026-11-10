from __future__ import annotations

import re
from datetime import date
from typing import Any, Sequence
from urllib.parse import urlparse

_EXPLICIT_PROMOTION_PATTERNS = (
    re.compile(r"[$€£]\s*\d+(?:[,.]\d+)*"),
    re.compile(r"\b\d+(?:\.\d+)?\s*%\s*(?:off|discount)\b", re.IGNORECASE),
    re.compile(r"\b(?:off|discount(?:ed)?(?:\s+by)?)\s*\d+(?:\.\d+)?\s*%\b", re.IGNORECASE),
    re.compile(r"\bsave\s+[$€£]?\s*\d+(?:[,.]\d+)*", re.IGNORECASE),
    re.compile(r"[$€£]\s*\d+(?:[,.]\d+)*\s*off\b", re.IGNORECASE),
    re.compile(r"\bbuy\b.{0,80}\bget\b.{0,40}\bfree\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bfree\b.{0,80}\b(?:with|when|purchase|buy)\b", re.IGNORECASE | re.DOTALL),
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _normalized_words(value: Any) -> str:
    return " ".join(_NON_WORD.sub(" ", str(value or "").casefold()).split())


def _contains_phrase(haystack: str, phrase: str) -> bool:
    return bool(phrase and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", haystack))


def has_explicit_promotion_signal(text: str) -> bool:
    """Require a concrete price, discount, or purchase-bound free offer."""
    evidence = str(text or "")
    return any(pattern.search(evidence) for pattern in _EXPLICIT_PROMOTION_PATTERNS)


def activity_from_dates(
    start_date: str | None,
    end_date: str | None,
    evidence: str,
    *,
    today: date | None = None,
) -> dict[str, bool | None]:
    """Resolve audit-only activity state without inventing incomplete dates."""
    current_day = today or date.today()
    if bool(start_date) != bool(end_date):
        return {"is_active": None, "date_review_required": True}
    if start_date and end_date:
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            return {"is_active": None, "date_review_required": True}
        if start > end:
            return {"is_active": None, "date_review_required": True}
        return {
            "is_active": start <= current_day <= end,
            "date_review_required": False,
        }
    return {"is_active": True, "date_review_required": False}


def _website_path(url: Any) -> str:
    raw = str(url or "").strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return "/" + parsed.path.strip("/").casefold() if parsed.path.strip("/") else ""


def assign_businesses(
    page_url: str,
    evidence: str,
    businesses: Sequence[dict[str, Any]],
) -> tuple[list[int], str]:
    """Strictly attribute a page when a host has multiple business records."""
    members = [dict(row) for row in businesses]
    if len(members) == 1:
        return [int(members[0]["business_id"])], "single_host_business"
    if not members:
        return [], "unmatched_host"

    page_path = _website_path(page_url)
    path_matches = [
        int(row["business_id"])
        for row in members
        if (path := _website_path(row.get("website"))) and (
            page_path == path or page_path.startswith(f"{path}/")
        )
    ]
    if path_matches:
        return sorted(set(path_matches)), "location_path"

    blob = _normalized_words(f"{page_url} {evidence}")
    address_matches = [
        int(row["business_id"])
        for row in members
        if _contains_phrase(blob, _normalized_words(row.get("address")))
    ]
    if address_matches:
        return sorted(set(address_matches)), "address"

    normalized_names = [_normalized_words(row.get("name")) for row in members]
    name_matches = [
        int(row["business_id"])
        for row, name in zip(members, normalized_names)
        if name and normalized_names.count(name) == 1 and _contains_phrase(blob, name)
    ]
    if name_matches:
        return sorted(set(name_matches)), "business_name"

    normalized_cities = [_normalized_words(row.get("city")) for row in members]
    city_matches = [
        int(row["business_id"])
        for row, city in zip(members, normalized_cities)
        if city and normalized_cities.count(city) == 1 and _contains_phrase(blob, city)
    ]
    if city_matches:
        return sorted(set(city_matches)), "city"
    return [], "shared_host_unresolved"
