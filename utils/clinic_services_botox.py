"""Extract Botox unit pricing from Firecrawl crawl markdown pages."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, List, Optional

from utils.offer_fingerprint import normalize_unit_type
from utils.offer_field_normalize import normalize_service_area

_BOTOX_RE = re.compile(r"\bbotox\b", re.IGNORECASE)
_UNIT_TOKEN = r"(?:unit|units|syringe|area|vial)"
# $11/unit, $11 per unit, 11 / unit
_PRICE_PER_UNIT_RE = re.compile(
    rf"\$?\s*(\d+(?:\.\d+)?)\s*(?:/|per)\s*({_UNIT_TOKEN})\b",
    re.IGNORECASE,
)
# Botox ... $11 ... (unit|syringe) within a short window — weaker signal
_PRICE_NEAR_UNIT_RE = re.compile(
    rf"\$\s*(\d+(?:\.\d+)?).{{0,40}}\b({_UNIT_TOKEN})\b"
    rf"|\b({_UNIT_TOKEN})\b.{{0,40}}\$\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)
_AREA_HINTS = (
    "forehead",
    "glabella",
    "crow's feet",
    "crows feet",
    "eleven lines",
    "11s",
    "lip flip",
    "masseter",
    "jawline",
    "neck",
    "bunny lines",
    "chin",
)
_PACKAGE_HINT_RE = re.compile(
    r"\b(?:package|bundle|combo|for\s+\d+\s+units?|units?\s+for)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BotoxServiceFields:
    regular_price: Optional[Decimal]
    unit_type: Optional[str]
    service_area: Optional[str]


def _to_decimal(value: str) -> Optional[Decimal]:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if amount <= 0 or amount > Decimal("1000"):
        # ponytail: unit Botox rarely > $1000; package totals filtered here
        return None
    return amount


def _window_around_botox(text: str, *, radius: int = 280) -> List[str]:
    windows: List[str] = []
    for match in _BOTOX_RE.finditer(text or ""):
        start = max(0, match.start() - radius)
        end = min(len(text), match.end() + radius)
        windows.append(text[start:end])
    return windows


def _pick_service_area(text: str) -> Optional[str]:
    lower = (text or "").lower()
    for hint in _AREA_HINTS:
        if hint in lower:
            return normalize_service_area(hint)
    return None


def extract_botox_fields_from_text(text: str) -> BotoxServiceFields:
    """Pull unit price / unit_type / service_area from markdown mentioning Botox."""
    if not text or not _BOTOX_RE.search(text):
        return BotoxServiceFields(None, None, None)

    candidates: List[tuple[int, Decimal, str, str]] = []
    # rank: 0 = strong $/unit near botox, 1 = weaker price+unit near botox
    for window in _window_around_botox(text):
        if _PACKAGE_HINT_RE.search(window) and not _PRICE_PER_UNIT_RE.search(window):
            continue
        for match in _PRICE_PER_UNIT_RE.finditer(window):
            price = _to_decimal(match.group(1))
            unit = normalize_unit_type(match.group(2))
            if price is None or not unit:
                continue
            candidates.append((0, price, unit, window))
        if candidates:
            continue
        for match in _PRICE_NEAR_UNIT_RE.finditer(window):
            if match.group(1) and match.group(2):
                price = _to_decimal(match.group(1))
                unit = normalize_unit_type(match.group(2))
            else:
                price = _to_decimal(match.group(4) or "")
                unit = normalize_unit_type(match.group(3) or "")
            if price is None or not unit:
                continue
            candidates.append((1, price, unit, window))

    if not candidates:
        return BotoxServiceFields(None, None, _pick_service_area(text))

    candidates.sort(key=lambda item: (item[0], item[1]))
    _, price, unit, window = candidates[0]
    return BotoxServiceFields(
        regular_price=price,
        unit_type=unit or None,
        service_area=_pick_service_area(window) or _pick_service_area(text),
    )


def extract_botox_fields_from_pages(pages: Iterable[Any]) -> BotoxServiceFields:
    """Merge crawl pages; prefer first strong unit-price hit."""
    best: Optional[BotoxServiceFields] = None
    for page in pages:
        if isinstance(page, dict):
            markdown = str(page.get("markdown") or page.get("content") or "")
        else:
            markdown = str(getattr(page, "markdown", None) or "")
        fields = extract_botox_fields_from_text(markdown)
        if fields.regular_price is None:
            if best is None and (fields.unit_type or fields.service_area):
                best = fields
            continue
        return fields
    return best or BotoxServiceFields(None, None, None)


def extract_botox_fields_from_search_pages(pages: Iterable[Any]) -> BotoxServiceFields:
    """Merge Firecrawl search pages (SearchPage or dict with markdown)."""
    dict_pages = []
    for page in pages:
        if hasattr(page, "markdown"):
            dict_pages.append(
                {
                    "markdown": getattr(page, "markdown", None),
                    "url": getattr(page, "url", None),
                    "title": getattr(page, "title", None),
                }
            )
        else:
            dict_pages.append(page)
    return extract_botox_fields_from_pages(dict_pages)


def website_to_crawl_url(website: Any) -> str:
    raw = str(website or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    return f"https://{raw.lstrip('/')}"
