"""Normalize promo offer price fields for write and backfill."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

_PRICE_TOKEN = re.compile(r"\$?\s*(\d+(?:\.\d+)?)")
_WAS_NOW = re.compile(
    r"(?:was|from|reg\.?)\s*\$?\s*(\d+(?:\.\d+)?).*?(?:now|sale|only|for)\s*\$?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE | re.DOTALL,
)
_ARROW_PAIR = re.compile(
    r"\$?\s*(\d+(?:\.\d+)?)\s*(?:→|->|to)\s*\$?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def parse_price(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    cleaned = re.sub(r"[,$]", "", str(value)).strip()
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _extract_prices_from_text(text: str) -> tuple[Optional[float], Optional[float]]:
    if not text:
        return None, None

    match = _WAS_NOW.search(text)
    if match:
        return parse_price(match.group(1)), parse_price(match.group(2))

    match = _ARROW_PAIR.search(text)
    if match:
        return parse_price(match.group(1)), parse_price(match.group(2))

    tokens = [parse_price(m.group(1)) for m in _PRICE_TOKEN.finditer(text)]
    prices = [p for p in tokens if p is not None]
    if len(prices) >= 2:
        return prices[0], prices[1]
    if len(prices) == 1:
        return prices[0], None
    return None, None


def _derive_discount_fields(
    regular: Optional[float],
    discount: Optional[float],
    amount: Optional[float],
    percent: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    if regular is None or discount is None or regular <= 0:
        return amount, percent

    computed_amount = round(regular - discount, 2)
    computed_percent = round((computed_amount / regular) * 100, 2)

    if amount is None:
        amount = computed_amount
    elif amount < 0 or amount > regular:
        amount = computed_amount

    if percent is None:
        percent = computed_percent
    elif percent < 0 or percent > 100:
        percent = computed_percent

    return amount, percent


def normalize_offer_prices(
    *,
    regular_price: Any = None,
    discount_price: Any = None,
    discount_amount: Any = None,
    discount_percent: Any = None,
    original_price: Any = None,
    offer_raw_text: Any = None,
) -> Dict[str, Optional[float]]:
    regular = parse_price(regular_price) or parse_price(original_price)
    discount = parse_price(discount_price)
    amount = parse_price(discount_amount)
    percent = parse_price(discount_percent)

    text_regular, text_discount = _extract_prices_from_text(str(offer_raw_text or ""))

    if regular is None and text_regular is not None:
        regular = text_regular
    if discount is None and text_discount is not None:
        discount = text_discount

    # Single price in text when both fields empty -> regular only.
    if regular is None and discount is None and text_regular is not None and text_discount is None:
        regular = text_regular

    if (
        regular is not None
        and discount is None
        and text_discount is not None
        and text_discount != regular
    ):
        discount = text_discount
    elif (
        discount is not None
        and regular is None
        and text_regular is not None
        and text_regular != discount
    ):
        regular = text_regular

    if regular is not None and discount is not None and discount > regular:
        regular, discount = discount, regular

    amount, percent = _derive_discount_fields(regular, discount, amount, percent)

    return {
        "regular_price": regular,
        "discount_price": discount,
        "discount_amount": amount,
        "discount_percent": percent,
    }
