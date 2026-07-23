"""Guards and normalization for clinic_services catalog prices."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from utils.clinic_services_search import is_article_service_url, url_path_score

_CURRENCY_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")
_UNIT_COUNT_RE = re.compile(
    r"(?:up\s+to\s+)?(\d+)\s*(?:u\b|units?\b)",
    re.IGNORECASE,
)
_PROMO_PRICE_SIGNAL = re.compile(
    r"\b(?:special|promo|promotion|july\s+special|limited\s+time|new\s+patient\s+offer|"
    r"first\s+\d+\s+units?\s+for|regular\s+\$?\d+.*special)\b",
    re.IGNORECASE,
)
_MARKET_AVERAGE_SIGNAL = re.compile(
    r"\b(?:typically\s+ranges?|on\s+average|market\s+average|regional\s+range|"
    r"industry\s+standard|costa\s+mesa\s+residents|orange\s+county\s+residents)\b",
    re.IGNORECASE,
)
_PER_UNIT_SIGNAL = re.compile(
    r"\$\s*\d+(?:\.\d+)?\s*(?:/|per)\s*(?:unit|units)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ServiceCatalogDecision:
    accepted: bool
    reason: str
    normalized_item: dict[str, Any] | None = None


def normalize_source_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def is_catalog_ineligible_url(source_url: str) -> bool:
    return url_path_score(normalize_source_url(source_url)) < 0


def should_replace_source_url(existing_url: str | None, new_url: str) -> bool:
    existing = normalize_source_url(existing_url or "")
    incoming = normalize_source_url(new_url)
    if not incoming:
        return False
    if not existing:
        return True
    return url_path_score(incoming) >= url_path_score(existing)


def _positive(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _price_window(text: str, price: float, *, radius: int = 220) -> str:
    evidence = str(text or "")
    needle = f"{price:g}"
    for match in _CURRENCY_RE.finditer(evidence):
        amount = _positive(match.group(1))
        if amount is None:
            continue
        if abs(amount - price) > 0.01 and abs(amount - round(price)) > 0.01:
            continue
        start = max(0, match.start() - radius)
        end = min(len(evidence), match.end() + radius)
        return evidence[start:end]
    return evidence


def infer_unit_count(text: str, price: float | None = None) -> tuple[int | None, bool]:
    """Return explicit unit count near a price mention; bool = upper-bound wording."""
    blob = _price_window(str(text or ""), price) if price is not None else str(text or "")
    match = _UNIT_COUNT_RE.search(blob)
    if not match:
        return None, False
    try:
        count = int(match.group(1))
    except (TypeError, ValueError):
        return None, False
    if count <= 0:
        return None, False
    upper_bound = bool(re.search(r"up\s+to\s+" + re.escape(match.group(1)), blob, re.I))
    return count, upper_bound


def derive_offer_item_pricing(
    offer: dict[str, Any],
    *,
    evidence: str = "",
) -> list[dict[str, Any]]:
    """Fill quantity and per-unit discount price on offer items when evidence is explicit."""
    items = [dict(item) for item in (offer.get("items") or [])]
    if not items:
        return items
    blob = "\n".join(
        part
        for part in (
            str(offer.get("offer_raw_text") or ""),
            str(evidence or ""),
            str(items[0].get("service_name") or ""),
        )
        if part
    )
    regular_total = _positive(offer.get("regular_price"))
    discount_total = _positive(offer.get("discount_price"))
    count, _upper = infer_unit_count(blob, regular_total or discount_total)
    if count is None and len(items) == 1:
        count, _upper = infer_unit_count(blob)
    if count is not None:
        for item in items:
            if item.get("quantity") is None:
                item["quantity"] = count
            if item.get("unit_price") is None and discount_total is not None:
                item["unit_price"] = round(discount_total / count, 4)
    return items


def normalize_service_catalog_item(
    item: dict[str, Any],
    *,
    source_url: str,
    evidence: str,
) -> ServiceCatalogDecision:
    """Normalize list-price rows before clinic_services upsert."""
    normalized = dict(item)
    source = normalize_source_url(source_url)
    if is_catalog_ineligible_url(source):
        return ServiceCatalogDecision(False, "ineligible_source_url")

    price = _positive(normalized.get("regular_price"))
    if price is None:
        return ServiceCatalogDecision(False, "missing_service_or_price")

    raw_name = str(normalized.get("service_name_raw") or normalized.get("service_name") or "").strip()
    if not raw_name:
        return ServiceCatalogDecision(False, "missing_service_or_price")

    raw_cf = raw_name.casefold()
    std_name = str(normalized.get("service_name") or "").strip()
    if std_name == "Botox" and "lip flip" in raw_cf:
        return ServiceCatalogDecision(False, "named_subtreatment_not_unit_catalog")

    evidence_text = str(evidence or "")
    if _MARKET_AVERAGE_SIGNAL.search(evidence_text):
        return ServiceCatalogDecision(False, "market_average_not_clinic_price")

    unit_type = str(normalized.get("unit_type") or "others").strip().lower() or "others"
    if unit_type == "package":
        return ServiceCatalogDecision(False, "package_not_catalog_unit_price")

    if unit_type in {"session", "area", "treatment", "package"} or _PROMO_PRICE_SIGNAL.search(
        evidence_text
    ):
        count, _upper = infer_unit_count(
            "\n".join(part for part in (raw_name, evidence_text) if part),
            price,
        )
        if count is not None and count >= 2:
            normalized["regular_price"] = round(price / count, 4)
            normalized["unit_type"] = "unit"
        elif unit_type in {"session", "area", "treatment"} and not _PER_UNIT_SIGNAL.search(evidence_text):
            return ServiceCatalogDecision(False, "non_unit_price_without_explicit_units")

    if normalized.get("unit_type") in (None, "", "others") and _PER_UNIT_SIGNAL.search(evidence_text):
        normalized["unit_type"] = "unit"

    return ServiceCatalogDecision(True, "validated", normalized_item=normalized)


def prepare_service_catalog_write(
    item: dict[str, Any],
    *,
    source_url: str,
    evidence: str,
    existing_source_url: str | None = None,
) -> ServiceCatalogDecision:
    decision = normalize_service_catalog_item(item, source_url=source_url, evidence=evidence)
    if not decision.accepted or decision.normalized_item is None:
        return decision
    if existing_source_url is not None and not should_replace_source_url(existing_source_url, source_url):
        return ServiceCatalogDecision(False, "weaker_source_url_than_existing")
    return decision
