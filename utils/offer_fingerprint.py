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


def offer_fingerprint_key(
    *,
    source_url: str,
    service_name: str,
    unit_type: Any = "",
) -> str:
    return "|".join(
        [
            normalize_url(source_url),
            normalize_service_name(service_name),
            normalize_unit_type(unit_type),
        ]
    )


def compute_offer_fingerprint(
    *,
    source_url: str,
    service_name: str,
    unit_type: Any = "",
) -> str:
    key = offer_fingerprint_key(
        source_url=source_url,
        service_name=service_name,
        unit_type=unit_type,
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()
