"""Normalize and compare promo_website_staging page_content values."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

PRICE_SIGNAL_PATTERNS = [
    re.compile(r"\$\s*\d+(?:,\d{3})*(?:\.\d{1,2})?", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?\s*%\s*(?:off|discount|savings?)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:price|pricing|starts? at|from|per unit|per syringe|membership|specials?|offers?|promo|deal|discount)\b",
        re.IGNORECASE,
    ),
]


def normalize_content(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\[SEGMENT\s+\d+\]\s*", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def content_hash(value: Any) -> str:
    normalized = normalize_content(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def has_price_signal(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in PRICE_SIGNAL_PATTERNS)


@dataclass(frozen=True)
class ContentChangeResult:
    change_type: str  # unchanged | changed | empty_old | empty_new | both_empty
    old_hash: str
    new_hash: str
    price_signal_lost: bool
    price_signal_gained: bool
    old_len: int
    new_len: int


def classify_content_change(old_content: str, new_content: str) -> ContentChangeResult:
    old_norm = normalize_content(old_content)
    new_norm = normalize_content(new_content)
    old_h = content_hash(old_content)
    new_h = content_hash(new_content)
    old_has_price = has_price_signal(old_norm)
    new_has_price = has_price_signal(new_norm)

    if not old_norm and not new_norm:
        change_type = "both_empty"
    elif not old_norm:
        change_type = "empty_old"
    elif not new_norm:
        change_type = "empty_new"
    elif old_h == new_h:
        change_type = "unchanged"
    else:
        change_type = "changed"

    return ContentChangeResult(
        change_type=change_type,
        old_hash=old_h,
        new_hash=new_h,
        price_signal_lost=old_has_price and not new_has_price,
        price_signal_gained=not old_has_price and new_has_price,
        old_len=len(str(old_content or "")),
        new_len=len(str(new_content or "")),
    )
