"""Normalize promo_offer_master text/bool fields."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from utils.offer_fingerprint import normalize_unit_type

_TRUTHY = {"true", "t", "yes", "y", "1"}
_FALSY = {"false", "f", "no", "n", "0"}
_UNSPECIFIED_AREAS = {"", "n/a", "na", "none", "not specified", "unknown", "unspecified", "general"}
_LOCATION_AREA_PATTERN = re.compile(r"\b(?:az|colorado|denver|boulder|fort collins|rino|tucson|oro valley|green valley|vail|marana|benson|casa grande|picture rocks|starr pass|catalina)\b", re.IGNORECASE)
_AREA_PATTERNS = (
    ("submental", re.compile(r"\b(?:submental|double chin|under(?:\s+the)?\s+chin|below the chin)\b", re.IGNORECASE)),
    ("under_eyes", re.compile(r"\b(?:under[ -]?eyes?|under eye)\b", re.IGNORECASE)),
    ("crow_feet", re.compile(r"\bcrow['’]?s? feet\b", re.IGNORECASE)),
    ("frown_lines", re.compile(r"\bfrown lines?\b", re.IGNORECASE)),
    ("forehead", re.compile(r"\bforehead\b", re.IGNORECASE)),
    ("jawline", re.compile(r"\bjawline\b", re.IGNORECASE)),
    ("trapezius", re.compile(r"\btrapezius\b", re.IGNORECASE)),
    ("underarms", re.compile(r"\bunderarms?\b", re.IGNORECASE)),
    ("earlobes", re.compile(r"\bearlobes?\b", re.IGNORECASE)),
    ("cheeks", re.compile(r"\bcheeks?\b", re.IGNORECASE)),
    ("lips", re.compile(r"\b(?:lips?|upper lip)\b", re.IGNORECASE)),
    ("chin", re.compile(r"\bchin\b", re.IGNORECASE)),
    ("hands", re.compile(r"\bhands?\b", re.IGNORECASE)),
    ("neck", re.compile(r"\bneck\b", re.IGNORECASE)),
    ("body", re.compile(r"\bbody\b", re.IGNORECASE)),
    ("nose", re.compile(r"\bnose\b", re.IGNORECASE)),
    ("nipple", re.compile(r"\bnipple\b", re.IGNORECASE)),
    ("face", re.compile(r"\b(?:face|facial)\b", re.IGNORECASE)),
)


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
    """Map free-form anatomy text to the clinic_services enum vocabulary."""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if text.casefold() in _UNSPECIFIED_AREAS or _LOCATION_AREA_PATTERN.search(text):
        return None
    matches = [name for name, pattern in _AREA_PATTERNS if pattern.search(text)]
    unique = list(dict.fromkeys(matches))
    if not unique:
        return None
    return unique[0] if len(unique) == 1 else "multiple_areas"


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
