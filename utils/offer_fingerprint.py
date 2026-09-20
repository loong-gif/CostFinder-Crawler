"""Offer identity fingerprint shared with ``fn_compute_offer_fingerprint``."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def normalize_url(url: str) -> str:
    """Normalize source URLs before they participate in an offer fingerprint."""
    parsed = urlparse(str(url or "").strip())
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    )
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            path=path,
            query=urlencode(query),
            fragment="",
        )
    )

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
    """Match the database trigger's ASCII token normalization exactly."""
    text = re.sub(r"[^a-zA-Z0-9]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().lower()


def offer_fingerprint_key(
    *,
    source_url: str,
    service_name: str,
    unit_type: Any = "",
    regular_price: Any = None,
    discount_price: Any = None,
    offer_raw_text: str = "",
    business_id: Any = None,
    promotion_id: Any = None,
    membership_plan_id: Any = None,
    discount_percent: Any = None,
    discount_amount: Any = None,
) -> str:
    del source_url, service_name, unit_type, regular_price, discount_price
    del discount_percent, discount_amount
    if membership_plan_id is not None:
        source_key = f"m:{membership_plan_id}"
    elif promotion_id is not None:
        source_key = f"p:{promotion_id}"
    else:
        source_key = f"b:{'' if business_id is None else business_id}"
    return f"{source_key}|{normalize_offer_raw_text(offer_raw_text)}"


def compute_offer_fingerprint(
    *,
    source_url: str,
    service_name: str,
    unit_type: Any = "",
    regular_price: Any = None,
    discount_price: Any = None,
    offer_raw_text: str = "",
    business_id: Any = None,
    promotion_id: Any = None,
    membership_plan_id: Any = None,
    discount_percent: Any = None,
    discount_amount: Any = None,
) -> str:
    key = offer_fingerprint_key(
        source_url=source_url,
        service_name=service_name,
        unit_type=unit_type,
        regular_price=regular_price,
        discount_price=discount_price,
        offer_raw_text=offer_raw_text,
        business_id=business_id,
        promotion_id=promotion_id,
        membership_plan_id=membership_plan_id,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()
