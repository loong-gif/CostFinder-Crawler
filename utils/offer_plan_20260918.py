"""Pure rules used by the source-backed Offer repair run on 2026-09-18."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


_NUMBER_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
}
_UNIT = r"units?|u(?:nit)?s?|syringes?|vials?|sessions?|treatments?|areas?"


def _fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _fold(value)
        if key and key not in seen:
            result.append(value)
            seen.add(key)
    return result


def _quantity_term(raw: str) -> str | None:
    numeric = r"(\d+(?:\.\d+)?)"
    patterns = (
        rf"\b(?:minimum|at\s+least)\s+{numeric}\s+({_UNIT})\b",
        rf"\b{numeric}\s+({_UNIT})\s+(?:minimum|required)\b",
        rf"\b{numeric}\s*\+\s*({_UNIT})\b",
        rf"\b{numeric}\s+({_UNIT})\s+or\s+more\b",
        rf"\b{numeric}\s+or\s+more\s+({_UNIT})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            number = match.group(1)
            unit = match.group(2).lower()
            normalized_number = str(float(number)).rstrip("0").rstrip(".") if "." in number else number
            return f"minimum {normalized_number} {unit}"

    word_pattern = rf"\b({'|'.join(_NUMBER_WORDS)})\s+or\s+more\s+({_UNIT})\b"
    match = re.search(word_pattern, raw, re.IGNORECASE)
    if match:
        return f"minimum {_NUMBER_WORDS[match.group(1).casefold()]} {match.group(2).lower()}"
    return None


def extract_explicit_terms(raw: Any) -> list[str]:
    """Extract only restrictions stated in the Offer text itself."""
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        return []
    terms: list[str] = []
    quantity = _quantity_term(text)
    if quantity:
        terms.append(quantity)
    code = re.search(r"\buse\s+code\s+([A-Z0-9][A-Z0-9_-]*)\b", text, re.IGNORECASE)
    if code:
        terms.append(f"promo code {code.group(1).upper()}")
    if re.search(
        r"\b(?:not\s+combinable|cannot|can't|can\s+not)\s+(?:be\s+combined|with)\b",
        text,
        re.IGNORECASE,
    ):
        terms.append("Cannot be combined with other offers")
    return _unique(terms)


def should_clear_per_unit_quantity(raw: Any) -> bool:
    """Return true when a stored ``1`` is only an implied per-unit default."""
    text = str(raw or "")
    if re.search(r"\bfirst\s+\d+(?:\.\d+)?\s+units?\b", text, re.IGNORECASE):
        return True
    if re.search(
        r"\b(?:\d+(?:\.\d+)?|one|two|three|four|five)\s+"
        r"(?:unit|units|syringe|syringes|vial|vials|session|sessions)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    return bool(
        re.search(
            r"(?:/|per\s+|\bextra\s+|\btox(?:in)?s?\s+)"
            r"(?:unit|units|syringe|syringes|vial|vials)\b",
            text,
            re.IGNORECASE,
        )
        or re.search(r"\b(?:extra|save)\b[^.\n]{0,50}\bunits?\b", text, re.IGNORECASE)
        or re.search(r"\beach\s+(?:unit|syringe|vial)s?\b", text, re.IGNORECASE)
    )


def split_offer_fingerprint(*, source_key: str, service_name: str, offer_raw_text: str, **fields: Any) -> str:
    """Fingerprint a split Offer without collapsing distinct service choices."""
    parts = [source_key, f"service:{_fold(service_name)}", f"raw:{_fold(offer_raw_text)}"]
    for key in ("regular_price", "discount_price", "discount_percent", "discount_amount", "price_model"):
        value = fields.get(key)
        parts.append(f"{key}:{'' if value is None else value}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
