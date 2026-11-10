from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import unicodedata
from typing import Any

from utils.offer_extraction_llm import canonicalize_service_name


PROMOTIONS = "clinic_promotions"
OFFERS = "promo_offer_master"
ITEMS = "promo_offer_items"
TERMS = "promo_offer_terms"
_PRICE_FIELDS = ("regular_price", "discount_price", "discount_percent", "discount_amount")
_MONEY = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s*%")


class _Review(ValueError):
    pass


def _text(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^\w]+", " ", value).strip()


def _raw_identity(value: Any) -> str:
    return _text(_MONEY.sub(" ", str(value or "")))


def _service_name_key(value: Any) -> str:
    canonical = canonicalize_service_name(value)
    return _text(canonical) if canonical and _text(canonical) != "others" else _text(value)


def _number(value: Any, field: str, *, positive: bool = False) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _Review(f"invalid_number:{field}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _Review(f"invalid_number:{field}") from None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise _Review(f"invalid_number:{field}")
    return number


def _rounded(value: Decimal, places: str = "0.01") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _id(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _Review("invalid_row_id")
    return value
