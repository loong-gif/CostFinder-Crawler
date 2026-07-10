"""Normalize promo_offer_master text/bool fields."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from utils.offer_fingerprint import normalize_unit_type

_TRUTHY = {"true", "t", "yes", "y", "1"}
_FALSY = {"false", "f", "no", "n", "0"}


def normalize_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def normalize_service_area(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text or None


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value).strip()


def prefer_longer_offer_raw_text(
    offer_raw_text: Any,
    offer: Dict[str, Any],
    *,
    min_len: int = 20,
) -> str:
    best = str(offer_raw_text or "").strip()
    for field in ("offer_content", "evidence_segments"):
        alt = _coerce_text(offer.get(field))
        if len(alt) > len(best) and len(alt) >= min_len:
            best = alt
    return best


def normalize_offer_field_values(
    payload: Dict[str, Any],
    *,
    offer: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a copy with normalized unit_type, service_area, bool columns."""
    out = dict(payload)

    if "unit_type" in out:
        normalized = normalize_unit_type(out.get("unit_type"))
        if normalized:
            out["unit_type"] = normalized
        elif out.get("unit_type") in ("", None):
            out.pop("unit_type", None)

    if "service_area" in out:
        area = normalize_service_area(out.get("service_area"))
        if area:
            out["service_area"] = area
        else:
            out.pop("service_area", None)

    for field in ("is_membership_required", "is_package"):
        if field not in out:
            continue
        parsed = normalize_bool(out.get(field))
        if parsed is None:
            out.pop(field, None)
        else:
            out[field] = parsed

    if offer is not None and "offer_raw_text" in out:
        longer = prefer_longer_offer_raw_text(out.get("offer_raw_text"), offer)
        if longer:
            out["offer_raw_text"] = longer

    return out
