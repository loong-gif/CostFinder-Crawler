"""Conservative, text-backed validation for promo offer price fields.

The planner never infers a discount from an unlabelled single price.  A field is
cleared only when ``offer_raw_text`` explicitly identifies a contradictory
price, discount amount, or discount percentage.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional


_NUMBER = r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?!\d|,\d)"
_REGULAR_TO_SALE = re.compile(
    rf"(?:was|regular|original|list)\s*(?:price)?\s*[:=]?\s*\$?\s*{_NUMBER}"
    rf".{0,100}?"
    rf"(?:now|sale|member|current|only)\s*(?:price)?\s*[:=]?\s*\$?\s*{_NUMBER}",
    re.IGNORECASE | re.DOTALL,
)
_ARROW_PAIR = re.compile(rf"\$?\s*{_NUMBER}\s*(?:→|->)\s*\$?\s*{_NUMBER}")
_LABELLED_PRICE = {
    "regular_price": re.compile(
        rf"(?:was|regular|original|list)\s*(?:price)?\s*[:=]?\s*\$?\s*{_NUMBER}",
        re.IGNORECASE,
    ),
    "discount_price": re.compile(
        rf"(?:now|sale|member|current|only|special\s+price)\s*(?:price)?\s*[:=]?\s*\$?\s*{_NUMBER}(?!\s*(?:off|discount)\b)",
        re.IGNORECASE,
    ),
}
_AMOUNT = re.compile(
    rf"save\s*\$\s*{_NUMBER}\b|"
    rf"\$\s*{_NUMBER}\s*(?:off|discount)\b|"
    rf"(?:save\s*)?{_NUMBER}\s*(?:dollars?|bucks?)\s*off\b",
    re.IGNORECASE,
)
_PERCENT = re.compile(rf"{_NUMBER}\s*%\s*(?:off|discount)\b", re.IGNORECASE)
_PER_UNIT_PAIR = re.compile(
    rf"\$?\s*{_NUMBER}\s*(?:/|per)\s*(?:u(?:nit)?s?)\b"
    rf".{{0,140}}?"
    rf"(?:regular(?:ly)?|reg\.?|non[-\s]?members?)\D{{0,30}}?\$?\s*{_NUMBER}\s*(?:/|per)\s*(?:u(?:nit)?s?)\b",
    re.IGNORECASE | re.DOTALL,
)


def _number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _different(actual: Any, expected: float) -> bool:
    value = _number(actual)
    return value is not None and abs(value - expected) > 0.011


def _first_number(match: re.Match[str]) -> Optional[float]:
    for value in match.groups():
        if value is not None:
            return _number(value)
    return None


def extract_price_evidence(offer_raw_text: Any) -> Dict[str, float]:
    """Extract only explicitly labelled price/discount evidence from source text."""
    text = str(offer_raw_text or "")
    evidence: Dict[str, float] = {}

    pair = _REGULAR_TO_SALE.search(text) or _ARROW_PAIR.search(text)
    if pair:
        regular, discount = _number(pair.group(1)), _number(pair.group(2))
        if regular is not None and discount is not None and regular > 0 and discount <= regular:
            evidence["regular_price"] = regular
            evidence["discount_price"] = discount
            evidence["discount_amount"] = round(regular - discount, 2)
            evidence["discount_percent"] = round((regular - discount) * 100 / regular, 2)

    per_unit_pair = _PER_UNIT_PAIR.search(text)
    if per_unit_pair:
        discount, regular = _number(per_unit_pair.group(1)), _number(per_unit_pair.group(2))
        if discount is not None and regular is not None and regular > 0 and 0 < discount <= regular:
            evidence["regular_price"] = regular
            evidence["discount_price"] = discount
            evidence["discount_amount"] = round(regular - discount, 2)
            evidence["discount_percent"] = round((regular - discount) * 100 / regular, 2)

    for field, pattern in _LABELLED_PRICE.items():
        match = pattern.search(text)
        if match:
            value = _number(match.group(1))
            if value is not None:
                evidence[field] = value

    amount = _AMOUNT.search(text)
    if amount and "discount_amount" not in evidence:
        value = _first_number(amount)
        if value is not None:
            evidence["discount_amount"] = value

    percent = _PERCENT.search(text)
    if percent and "discount_percent" not in evidence:
        value = _number(percent.group(1))
        if value is not None:
            evidence["discount_percent"] = value

    return evidence


def plan_inconsistent_price_field_clears(row: Mapping[str, Any]) -> Dict[str, None]:
    """Return fields to clear when explicit source text contradicts stored values.

    For amount/percent-only evidence, both stored prices must support a concrete
    calculation before a contradictory derived value is cleared.
    """
    evidence = extract_price_evidence(row.get("offer_raw_text"))
    clears: Dict[str, None] = {}
    for field in ("regular_price", "discount_price"):
        expected = evidence.get(field)
        if expected is not None and _different(row.get(field), expected):
            clears[field] = None

    regular = _number(row.get("regular_price"))
    discount = _number(row.get("discount_price"))
    has_stored_pair = regular is not None and discount is not None and regular > 0 and 0 <= discount <= regular
    for field in ("discount_amount", "discount_percent"):
        expected = evidence.get(field)
        if expected is None or not _different(row.get(field), expected):
            continue
        if field in {"discount_amount", "discount_percent"} and field not in evidence:
            continue
        if field in evidence and (
            "regular_price" in evidence or "discount_price" in evidence or has_stored_pair
        ):
            clears[field] = None

    # An explicit "save $X" / "N% off" plus one stored price determines the
    # other price. Clear only the contradictory stored value; never fill it.
    raw_text = str(row.get("offer_raw_text") or "")
    has_quantity = bool(re.search(r"\b\d+\s+(?:units?|vials?|syringes?|treatments?)\b", raw_text, re.I))
    amount = evidence.get("discount_amount")
    if amount is not None:
        if regular is not None and regular >= amount:
            expected_discount = regular - amount
            if _different(row.get("discount_price"), expected_discount):
                clears["discount_price"] = None
        elif discount is not None and not has_quantity:
            expected_regular = discount + amount
            if _different(row.get("regular_price"), expected_regular):
                clears["regular_price"] = None

    percent = evidence.get("discount_percent")
    if percent is not None and 0 <= percent <= 100:
        if regular is not None:
            expected_discount = round(regular * (100 - percent) / 100, 2)
            if _different(row.get("discount_price"), expected_discount):
                clears["discount_price"] = None
        elif discount is not None and percent < 100 and not has_quantity:
            expected_regular = round(discount * 100 / (100 - percent), 2)
            if _different(row.get("regular_price"), expected_regular):
                clears["regular_price"] = None
    return clears
