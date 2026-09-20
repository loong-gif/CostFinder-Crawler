"""Evidence-gated Firecrawl extraction for the clinic services catalog."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from utils.clinic_services_search import url_path_score
from utils.clinic_service_taxonomy import service_name_enum_hint

FALLBACK_TARGET_RULE_CATEGORIES = {"neurotoxin", "filler", "skin_booster", "fat_dissolver"}
FALLBACK_CANONICAL_SERVICE_NAMES = {
    "Botox": "Botox",
    "Dysport": "Dysport",
    "Xeomin": "Xeomin",
    "Jeuveau": "Jeuveau",
    "Daxxify": "Daxxify",
    "Letybo": "Letybo",
    "Neurotoxin": "Neurotoxin",
    "Juvederm": "Juvéderm",
    "Restylane": "Restylane",
    "Belotero": "Belotero",
    "Dermal Filler": "Dermal Filler",
    "Radiesse": "Radiesse",
    "SkinVive": "Skinvive",
}
FALLBACK_EVIDENCE_PATTERNS = {
    "Botox": re.compile(r"\bbotox\b", re.IGNORECASE),
    "Dysport": re.compile(r"\bdysport\b", re.IGNORECASE),
    "Xeomin": re.compile(r"\bxeomin\b", re.IGNORECASE),
    "Jeuveau": re.compile(r"\bjeuveau\b", re.IGNORECASE),
    "Daxxify": re.compile(r"\bdaxxify\b", re.IGNORECASE),
    "Letybo": re.compile(r"\bletybo\b", re.IGNORECASE),
    "Neurotoxin": re.compile(r"\bneurotox(?:in|ins?)\b|\bwrinkle relaxers?\b", re.IGNORECASE),
    "Juvéderm": re.compile(r"\bjuv[ée]derm\b|\bvoluma\b|\bvolux\b|\bvolbella\b|\bvollure\b", re.IGNORECASE),
    "Restylane": re.compile(r"\brestylane\b", re.IGNORECASE),
    "Belotero": re.compile(r"\bbelotero\b", re.IGNORECASE),
    "Dermal Filler": re.compile(r"\b(?:dermal\s+)?fillers?\b", re.IGNORECASE),
    "Radiesse": re.compile(r"\bradiesse\b", re.IGNORECASE),
    "Skinvive": re.compile(r"\bskinvive\b", re.IGNORECASE),
}
ALLOWED_UNITS = {"unit", "syringe", "half_syringe", "vial", "treatment", "session", "area", "ml", "mg", "others"}
UNSPECIFIED_VALUES = {"", "n/a", "na", "none", "not applicable", "not specified", "unknown", "unspecified"}
PROMOTIONAL_PRICE_TOKENS = re.compile(
    r"\b(?:special|promo(?:tional)?|sale|discount|member(?:ship)?|new\s+(?:patient|client|guest)|"
    r"first\s+(?:time|visit)|limited\s+time|save\s+|off\s+)\b|\b(?:from|starting\s+at)\b",
    re.IGNORECASE,
)
PRICE_TOKEN = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)\b")
UNIT_TOKEN = re.compile(r"\b(?:per|/)\s*(unit|units|syringe|half[ -]?syringe|vial|treatment|session|area|ml|mg)\b", re.IGNORECASE)


@dataclass(frozen=True)
class CandidatePage:
    url: str
    score: int


def extract_rule_based_services(evidence: str) -> list[dict[str, Any]]:
    """Conservatively extract explicit target-service mentions without an LLM.

    This is only a quota-outage fallback. It does not infer a price: a positive
    price must occur on the same evidence line as precisely one target entity,
    and common promotional/from-price language is excluded.
    """
    services: dict[str, dict[str, Any]] = {}
    for raw_line in re.split(r"[\r\n]+", evidence or ""):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or len(line) > 900:
            continue
        matches = [(name, pattern) for name, pattern in FALLBACK_EVIDENCE_PATTERNS.items() if pattern.search(line)]
        unique = {name.casefold(): (name, pattern) for name, pattern in matches}
        for canonical_name, _pattern in unique.values():
            price: float | None = None
            unit_type = "others"
            # A line listing multiple individual products with one price is ambiguous.
            if len(unique) == 1 and not PROMOTIONAL_PRICE_TOKENS.search(line):
                price_match = PRICE_TOKEN.search(line)
                if price_match:
                    price = float(price_match.group(1))
                    unit_match = UNIT_TOKEN.search(line)
                    if unit_match:
                        unit_type = unit_match.group(1).lower().replace(" ", "_").replace("-", "_")
                        if unit_type == "units":
                            unit_type = "unit"
            item = {
                "service_name": canonical_name,
                "service_name_raw": line,
                "regular_price": price,
                "unit_type": unit_type,
                "service_area": "",
            }
            prior = services.get(canonical_name.casefold())
            if prior is None or (prior["regular_price"] is None and price is not None):
                services[canonical_name.casefold()] = item
    return list(services.values())


def build_queries(domain: str) -> list[str]:
    return [
        f"botox dysport xeomin pricing site:{domain}",
        f"dermal filler juvederm restylane pricing site:{domain}",
        f"injectables services menu site:{domain}",
    ]


def build_fallback_urls(website: str) -> list[CandidatePage]:
    """Provide bounded public paths when domain search returns no usable results."""
    base = website.strip().rstrip("/")
    if not base:
        return []
    if not base.startswith(("http://", "https://")):
        base = f"https://{base}"
    return [
        CandidatePage(url=base, score=0),
        CandidatePage(url=urljoin(f"{base}/", "services"), score=1),
        CandidatePage(url=urljoin(f"{base}/", "pricing"), score=1),
        CandidatePage(url=urljoin(f"{base}/", "injectables"), score=1),
    ]


def rank_candidate_urls(hits: Iterable[dict[str, Any]], *, domain: str, limit: int = 4) -> list[CandidatePage]:
    pages: dict[str, CandidatePage] = {}
    for hit in hits:
        url = str(hit.get("url") or "").strip().rstrip("/")
        if not url:
            continue
        score = url_path_score(url)
        if score < 0:
            continue
        page = CandidatePage(url=url, score=score)
        if url not in pages or page.score > pages[url].score:
            pages[url] = page
    def priority(page: CandidatePage) -> tuple[int, int, str]:
        path = urlparse(page.url).path.lower()
        if any(token in path for token in ("services", "injectable", "treatment")):
            page_type = 3
        elif any(token in path for token in ("pricing", "price", "menu")):
            page_type = 2
        else:
            page_type = 1
        return (-page_type, -page.score, page.url)

    return sorted(pages.values(), key=priority)[:limit]


def price_is_supported_by_raw_text(price: float, raw_text: str) -> bool:
    """Require the claimed price to appear in the service-specific evidence."""
    if price.is_integer():
        amount_pattern = rf"{int(price)}(?:\\.0{{1,2}})?"
    else:
        amount_pattern = re.escape(f"{price:.2f}".rstrip("0").rstrip(".")) + r"0?"
    return bool(re.search(rf"\$\s*{amount_pattern}\b", raw_text))


def validate_service(item: dict[str, Any], *, source_url: str, evidence: str) -> tuple[dict[str, Any] | None, str]:
    source_category = str(item.get("service_category_raw") or item.get("service_category") or "").strip()
    raw_name = str(item.get("service_name_raw") or "").strip()
    service_name = str(item.get("service_name") or "").strip()
    canonical_service_name = service_name_enum_hint(service_name)
    if not source_url.startswith(("http://", "https://")) or url_path_score(source_url) < 0:
        return None, "ineligible_source_url"
    if not raw_name or not service_name or raw_name.casefold() not in evidence.casefold():
        return None, "missing_service_evidence"
    price = item.get("regular_price")
    if price is not None:
        if isinstance(price, bool):
            return None, "invalid_regular_price"
        try:
            price = float(price)
        except (TypeError, ValueError):
            return None, "invalid_regular_price"
        if price <= 0:
            return None, "invalid_regular_price"
        if not price_is_supported_by_raw_text(price, raw_name):
            return None, "price_not_supported_by_evidence"
    unit_type = str(item.get("unit_type") or "").strip().lower()
    if unit_type in UNSPECIFIED_VALUES:
        unit_type = "others"
    if unit_type not in ALLOWED_UNITS:
        return None, "invalid_unit_type"
    if canonical_service_name is None:
        return None, "unsupported_service_name"
    normalized = {
        "service_name": canonical_service_name,
        "service_name_raw": raw_name,
        "regular_price": price,
        "unit_type": unit_type,
        "service_area": None if str(item.get("service_area") or "").strip().lower() in UNSPECIFIED_VALUES else str(item.get("service_area") or "").strip() or None,
        "source_url": source_url.rstrip("/"),
    }
    if source_category:
        normalized["service_category"] = source_category
    return normalized, "accepted"


def choose_best_services(items: Iterable[dict[str, Any],], *, source_url: str, evidence: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    for item in items:
        normalized, reason = validate_service(item, source_url=source_url, evidence=evidence)
        if normalized is None:
            rejected.append({"item": item, "reason": reason})
            continue
        key = service_name_enum_hint(normalized["service_name"]) or normalized["service_name"].casefold()
        current = accepted.get(key)
        if current is None or (current["regular_price"] is None and normalized["regular_price"] is not None):
            accepted[key] = normalized
    return list(accepted.values()), rejected
