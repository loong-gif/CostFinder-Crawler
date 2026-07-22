"""Stable offer identity fingerprint for promo_offer_master dedup."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from utils.offer_evidence_segments import normalize_url

_UNIT_ALIASES = {"units": "unit", "unit": "unit"}


def normalize_service_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_unit_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return _UNIT_ALIASES.get(text, text)


def normalize_price(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value).strip()


def normalize_offer_raw_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text[:240]


def offer_fingerprint_key(
    *,
    source_url: str,
    service_name: str,
    unit_type: Any = "",
    regular_price: Any = None,
    discount_price: Any = None,
    offer_raw_text: str = "",
) -> str:
    return "|".join(
        [
            normalize_url(source_url),
            normalize_service_name(service_name),
            normalize_unit_type(unit_type),
            normalize_price(regular_price),
            normalize_price(discount_price),
            normalize_offer_raw_text(offer_raw_text),
        ]
    )


def compute_offer_fingerprint(
    *,
    source_url: str,
    service_name: str,
    unit_type: Any = "",
    regular_price: Any = None,
    discount_price: Any = None,
    offer_raw_text: str = "",
) -> str:
    key = offer_fingerprint_key(
        source_url=source_url,
        service_name=service_name,
        unit_type=unit_type,
        regular_price=regular_price,
        discount_price=discount_price,
        offer_raw_text=offer_raw_text,
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()
