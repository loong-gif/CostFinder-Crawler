"""Plan safe normalization of offer-total and item-unit promo prices."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any


# The live promo_offer_items.unit_price column is numeric(12,4).
# Do not propose a value that the database would round into an invalid total.
_PRECISION = Decimal("0.0001")


@dataclass(frozen=True)
class OfferItemPricePlan:
    """Independent, reviewable master and item updates."""

    master_updates: list[dict[str, Any]]
    updates: list[dict[str, Any]]
    skipped: list[dict[str, Any]]


def _positive_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def _number(value: Decimal) -> float:
    return float(value)


def _same_price(left: Any, right: Decimal) -> bool:
    parsed = _positive_decimal(left)
    return parsed is not None and parsed == right


def build_offer_item_price_plan(rows: list[dict[str, Any]]) -> OfferItemPricePlan:
    """Return updates needed for ``discount_price = unit_price * quantity``.

    Different-item packages have no unambiguous price allocation, so their item
    unit prices are cleared and the master price is left untouched. Duplicate
    rows for the same linked service are treated as one logical item and use the
    sum of their quantities. A ``per_unit`` master stores a per-unit price and
    must remain that way; its item unit price simply equals ``discount_price``.
    """
    master_updates: list[dict[str, Any]] = []
    item_updates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for row in rows:
        offer_id = row.get("id")
        items = row.get("promo_offer_items")
        if not isinstance(items, list) or not items:
            skipped.append({"offer_id": offer_id, "reason": "missing_offer_items"})
            continue

        service_ids = {item.get("service_id") for item in items}
        if any("service_id" in item and item.get("service_id") is None for item in items):
            for item in items:
                if item.get("offer_item_id") is not None and item.get("unit_price") is not None:
                    item_updates.append({"offer_item_id": item["offer_item_id"], "unit_price": None})
            skipped.append({"offer_id": offer_id, "reason": "unlinked_item"})
            continue
        same_linked_service = len(items) > 1 and None not in service_ids and len(service_ids) == 1
        if len(items) > 1 and not same_linked_service:
            for item in items:
                if item.get("offer_item_id") is not None and item.get("unit_price") is not None:
                    item_updates.append({"offer_item_id": item["offer_item_id"], "unit_price": None})
            skipped.append({"offer_id": offer_id, "reason": "multi_item_package"})
            continue

        price_model = str(row.get("price_model") or "").strip().lower()
        if price_model == "from":
            skipped.append({"offer_id": offer_id, "reason": "starting_at_price"})
            continue
        if price_model not in {"total", "per_unit"}:
            skipped.append({"offer_id": offer_id, "reason": "unsupported_price_model"})
            continue

        if any(item.get("offer_item_id") is None for item in items):
            skipped.append({"offer_id": offer_id, "reason": "missing_offer_item_id"})
            continue
        discount_price = _positive_decimal(row.get("discount_price"))
        if discount_price is None:
            skipped.append({"offer_id": offer_id, "reason": "missing_positive_discount_price"})
            continue

        if price_model == "per_unit":
            unit_price = discount_price
        else:
            quantities = [_positive_decimal(item.get("quantity")) for item in items]
            if any(quantity is None for quantity in quantities):
                skipped.append({"offer_id": offer_id, "reason": "missing_positive_quantity"})
                continue
            quantity = sum(quantities, Decimal(0))
            rounded_unit_price = (discount_price / quantity).quantize(_PRECISION, rounding=ROUND_HALF_UP)
            if rounded_unit_price * quantity != discount_price:
                skipped.append({"offer_id": offer_id, "reason": "non_terminating_unit_price"})
                continue
            unit_price = rounded_unit_price

        for item in items:
            if not _same_price(item.get("unit_price"), unit_price):
                item_updates.append({"offer_item_id": item["offer_item_id"], "unit_price": _number(unit_price)})

    return OfferItemPricePlan(master_updates, item_updates, skipped)
