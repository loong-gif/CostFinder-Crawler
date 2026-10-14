"""Text-backed repair planning for promo-offer price semantics.

This module proposes source-backed master prices. Item unit-price arithmetic
can validate a stated total and delivered quantity; it cannot manufacture a
master price. Historical arithmetic-only master prices remain review cases.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any, Mapping

from utils.service_price_guard import quantity_has_bound


_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_QUANTITY_RE = re.compile(
    rf"(?P<number>{_NUMBER})\s+(?:[A-Za-z][A-Za-z0-9®-]*\s+)?(?P<kind>units?|u\b|syringes?|vials?|sessions?|treatments?)\b",
    re.IGNORECASE,
)
_UNIT_PRICE_RE = re.compile(
    rf"\$?\s*(?P<value>{_NUMBER})\s*(?:/|per)\s*(?:u\b|units?|syringes?|vials?)\b",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(rf"\$\s*(?P<value>{_NUMBER})", re.IGNORECASE)


@dataclass(frozen=True)
class SemanticRepairPlan:
    master_updates: list[dict[str, Any]]
    item_updates: list[dict[str, Any]]
    review_cases: list[dict[str, Any]]


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _positive(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _unit(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _same(left: Any, right: Any) -> bool:
    left_decimal = _decimal(left)
    return left_decimal == right if left_decimal is not None and right is not None else left_decimal is right


def _quantity_candidates(raw: str) -> list[tuple[int, str, int]]:
    candidates: list[tuple[int, str, int]] = []
    for match in _QUANTITY_RE.finditer(raw):
        number_text = match.group("number").replace(",", "")
        if "." in number_text or raw[max(0, match.start() - 1) : match.start()] == "$":
            continue
        number = int(number_text)
        if number <= 0:
            continue
        before = raw[max(0, match.start() - 4) : match.start()]
        if re.search(r"\d\s*[-–]\s*$", before):
            continue
        candidates.append((number, match.group("kind").casefold(), match.start()))
    return candidates


def _explicit_quantity(raw: str) -> int | None:
    if any(
        quantity_has_bound(raw[max(0, match.start() - 35):match.start()], raw[match.end():])
        for match in _QUANTITY_RE.finditer(raw)
    ):
        return None
    parenthesized_units = re.search(rf"\(\s*({_NUMBER})\s+units?\s*\)", raw, re.IGNORECASE)
    if parenthesized_units:
        return int(parenthesized_units.group(1).replace(",", ""))
    candidates = _quantity_candidates(raw)
    if not candidates:
        return None
    if re.search(r"select\s+package\s+size|\b(?:from|starting)\b", raw, re.IGNORECASE):
        return None
    delivered_total = re.search(
        rf"\b({_NUMBER})\s*(?:units?|syringes?|vials?|sessions?|treatments?)\s+for\s+(?:just\s+|only\s+)?\$\s*{_NUMBER}",
        raw,
        re.IGNORECASE,
    )
    if delivered_total:
        return int(delivered_total.group(1).replace(",", ""))
    stated_total = re.search(rf"\b({_NUMBER})\s*(?:units?|syringes?|vials?)\s+total\b", raw, re.IGNORECASE)
    if stated_total:
        return int(stated_total.group(1).replace(",", ""))
    minimum_total = re.search(
        rf"\b(?:minimum|min)\s+({_NUMBER})\s*(?:units?|syringes?|vials?)\b.*?(?:-|–|=)\s*\$\s*{_NUMBER}",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if minimum_total:
        return int(minimum_total.group(1).replace(",", ""))
    if re.search(r"\b(?:minimum|min|at\s+least|more\s+than|first)\b|\b(?:or\s+more|or\s+fewer)\b|\d+\s*\+\s*(?:units?|syringes?|vials?)", raw, re.IGNORECASE):
        return None
    kinds = {kind.rstrip("s") for _, kind, _ in candidates}
    if len(candidates) == 1:
        return candidates[0][0]
    if len(kinds) > 1:
        return None
    # Repeated same-unit mentions are a delivered total only for an explicit
    # free/add-on combination; duplicate rendering of the same quantity uses
    # one value.
    numbers = [number for number, _, _ in candidates]
    if len(set(numbers)) == 1:
        return numbers[0]
    if re.search(r"\b(?:plus|and|\+).*\bfree\b|\bfree\b.*\b(?:plus|and|\+)", raw, re.IGNORECASE):
        return sum(numbers)
    return None


def _unit_price(raw: str) -> Decimal | None:
    matches = list(_UNIT_PRICE_RE.finditer(raw))
    if not matches:
        return None
    unlabelled: list[Decimal] = []
    for match in matches:
        prefix = raw[max(0, match.start() - 24) : match.start()]
        if re.search(r"regular(?:ly)?|original|list|reg\.? ?$", prefix, re.IGNORECASE):
            continue
        value = _decimal(match.group("value"))
        if value is not None:
            unlabelled.append(value)
    return unlabelled[-1] if unlabelled else None


def _regular_unit_price(raw: str) -> Decimal | None:
    for match in _UNIT_PRICE_RE.finditer(raw):
        prefix = raw[max(0, match.start() - 30) : match.start()]
        if re.search(r"regular(?:ly)?|original|list|reg\.?", prefix, re.IGNORECASE):
            return _decimal(match.group("value"))
    return None


def _amount_only(raw: str) -> Decimal | None:
    patterns = (
        rf"\$\s*({_NUMBER})\s*(?:off|discount|savings?|rebate|credit)\b",
        rf"\b(?:save|savings?|rebate|credit)\s*\$\s*({_NUMBER})(?!\s*(?:/|per)\s*(?:u\b|units?|syringes?|vials?))\b",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            return _decimal(match.group(1))
    return None


def _percent_only(raw: str) -> Decimal | None:
    match = re.search(rf"({_NUMBER})\s*%\s*(?:off|discount)\b", raw, re.IGNORECASE)
    return _decimal(match.group(1)) if match else None


def _has_add_on(raw: str) -> bool:
    return bool(re.search(r"\badd\s*\$\s*" + _NUMBER, raw, re.IGNORECASE))


def _value_price(raw: str) -> Decimal | None:
    match = re.search(rf"\$\s*({_NUMBER})\s*value\b", raw, re.IGNORECASE)
    if match and re.search(r"\bup\s+to\s*$", raw[max(0, match.start() - 12) : match.start()], re.IGNORECASE):
        return None
    return _decimal(match.group(1)) if match else None


def _regular_total(raw: str, quantity: int | None, regular_unit: Decimal | None) -> Decimal | None:
    match = re.search(rf"\b(?:regular|original|list)\s*(?:price)?\s*[:=]?\s*\$?\s*({_NUMBER})", raw, re.IGNORECASE)
    if match:
        suffix = raw[match.end() : match.end() + 24]
        if re.search(r"(?:/|per)\s*(?:unit|units?)", suffix, re.IGNORECASE):
            return None
        return _decimal(match.group(1))
    value = _value_price(raw)
    if value is not None:
        return value
    return None


def _total_price(raw: str) -> Decimal | None:
    if _amount_only(raw) is not None or _has_add_on(raw):
        return None
    candidates: list[tuple[int, Decimal]] = []
    patterns = (
        rf"\bfor\s+(?:just\s+|only\s+)?\$\s*({_NUMBER})",
        rf"\b(?:only|special\s+price|sale\s+price|current\s+price)\s*[:=]?\s*\$\s*({_NUMBER})",
        rf"\b(?:your|member|sale|current)\s+price\s*[:=]?\s*\$\s*({_NUMBER})",
        rf"\|\s*\$\s*({_NUMBER})",
        rf"=\s*\$\s*({_NUMBER})",
        rf"(?:-|–)\s*\$\s*({_NUMBER})",
        rf"\$\s*({_NUMBER})\s+per\s+treatments?\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, raw, re.IGNORECASE):
            end = match.end()
            suffix = raw[end : end + 14]
            if re.match(r"\s*(?:/|per)\s*(?:u\b|units?|syringes?|vials?)", suffix, re.IGNORECASE):
                continue
            candidates.append((match.start(), _decimal(match.group(1))))
    if candidates:
        candidates = [(position, value) for position, value in candidates if value is not None]
        if candidates:
            if (
                len(_MONEY_RE.findall(raw)) > 1
                and re.search(r"(?:-|–)\s*\$", raw)
                and not re.search(r"\bvalue\b|\b(?:for|your\s+price|only|sale\s+price|current\s+price)\b|\|", raw, re.IGNORECASE)
            ):
                return None
            return max(candidates, key=lambda entry: entry[0])[1]
    # A quantity immediately followed by a money amount is a total even when
    # the source omits “for”.
    for quantity_match in _QUANTITY_RE.finditer(raw):
        number_text = quantity_match.group("number")
        if "." in number_text or raw[max(0, quantity_match.start() - 1) : quantity_match.start()] == "$":
            continue
        tail = raw[quantity_match.end() : quantity_match.end() + 90]
        money = _MONEY_RE.search(tail)
        if money and re.search(r"value\b", tail[max(0, money.start() - 12) : money.end() + 12], re.IGNORECASE):
            continue
        if money and not re.match(r"\s*(?:/|per)\s*(?:u\b|units?)", tail[money.end() :], re.IGNORECASE):
            candidates.append((quantity_match.start(), _decimal(money.group("value"))))
    candidates = [(position, value) for position, value in candidates if value is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: entry[0])[1]


def _is_multi_item(row: Mapping[str, Any], raw: str) -> bool:
    items = row.get("promo_offer_items")
    if isinstance(items, list) and len(items) > 1:
        service_ids = {item.get("service_id") for item in items}
        if len(service_ids) > 1 or None in service_ids:
            return True
    if re.search(r"\b\d+\s*(?:vials?|syringes?)\b.*(?:&|\+|\band\b).*\b\d+\s*(?:vials?|syringes?|units?)\b", raw, re.IGNORECASE):
        return True
    return False


def _changed_fields(row: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for field, value in expected.items():
        current = row.get(field)
        if field in {"regular_price", "discount_price", "discount_amount", "discount_percent"}:
            if _decimal(current) != _decimal(value):
                patch[field] = value
        elif current != value:
            patch[field] = value
    return patch


def build_semantic_repair_plan(rows: list[Mapping[str, Any]]) -> SemanticRepairPlan:
    master_updates: list[dict[str, Any]] = []
    item_updates: list[dict[str, Any]] = []
    review_cases: list[dict[str, Any]] = []

    for source_row in rows:
        row = dict(source_row)
        raw = str(row.get("offer_raw_text") or "")
        items = row.get("promo_offer_items") if isinstance(row.get("promo_offer_items"), list) else []
        quantity = _explicit_quantity(raw)
        unit_price = _unit_price(raw)
        regular_unit = _regular_unit_price(raw)
        total_price = _total_price(raw)
        amount_only = _amount_only(raw)
        percent_only = _percent_only(raw)
        value_price = _value_price(raw)
        multi_item = _is_multi_item(row, raw)
        explicit_total = total_price is not None

        arithmetic_only = total_price is None and unit_price is not None and quantity is not None and not multi_item
        if arithmetic_only:
            review_cases.append({"offer_id": row.get("id"), "rule_name": "arithmetic_only_master_price", "raw_text": raw})

        expected_master: dict[str, Any] = {}
        has_computable_total = total_price is not None
        amount_only_is_unambiguous = amount_only is not None and len(_MONEY_RE.findall(raw)) <= 1
        if amount_only_is_unambiguous and not has_computable_total:
            expected_master.update({"discount_price": None, "discount_amount": _money(amount_only), "discount_percent": None})
        elif percent_only is not None and not has_computable_total:
            expected_master.update({"discount_price": None, "discount_amount": None, "discount_percent": _money(percent_only)})
        elif _has_add_on(raw):
            expected_master.update({"discount_price": None, "discount_amount": None, "discount_percent": None})
        elif total_price is not None and not multi_item:
            expected_master["discount_price"] = _money(total_price)
        elif unit_price is not None and quantity is None:
            expected_master["discount_price"] = None

        regular_total = _regular_total(raw, quantity, regular_unit)
        if regular_total is None and value_price is not None:
            regular_total = value_price
        if regular_total is not None:
            expected_master["regular_price"] = _money(regular_total)

        final_discount = _decimal(expected_master.get("discount_price", row.get("discount_price")))
        final_regular = _decimal(expected_master.get("regular_price", row.get("regular_price")))
        if final_regular is not None and final_discount is not None and final_discount < final_regular:
            expected_master["discount_amount"] = _money(final_regular - final_discount)
            expected_master["discount_percent"] = _money((final_regular - final_discount) * 100 / final_regular)
        elif amount_only is None and percent_only is None and (_has_add_on(raw) or final_discount is None):
            if "discount_amount" not in expected_master and ("/unit" in raw.lower() or " per unit" in raw.lower()):
                expected_master["discount_amount"] = None
                expected_master["discount_percent"] = None

        if total_price is not None or amount_only_is_unambiguous or _has_add_on(raw):
            expected_master["price_model"] = "total"
        elif unit_price is not None and quantity is None:
            expected_master["price_model"] = "per_unit"

        # A stored regular/discount value that is smaller than the newly
        # recovered offer total is a unit-price leak, not a valid offer total.
        if total_price is not None and expected_master.get("regular_price") is None:
            current_regular = _positive(row.get("regular_price"))
            if current_regular is not None and current_regular < total_price:
                expected_master["regular_price"] = None
                expected_master["discount_amount"] = None
                expected_master["discount_percent"] = None
        if total_price is not None and regular_total is None:
            current_regular = _positive(row.get("regular_price"))
            if current_regular is not None and current_regular <= total_price:
                expected_master["regular_price"] = None
                expected_master["discount_amount"] = None
                expected_master["discount_percent"] = None
        if unit_price is not None and quantity is None:
            current_regular = _positive(row.get("regular_price"))
            if current_regular is not None and current_regular == unit_price:
                expected_master["regular_price"] = None

        # The live schema requires an active offer to retain at least one
        # discount field. A lone per-unit source with no quantity cannot be
        # represented in master without inventing a discount, so keep the
        # existing legal master value and send it to review.
        unit_only_unrepresentable = (
            unit_price is not None
            and quantity is None
            and total_price is None
            and amount_only_is_unambiguous is False
            and percent_only is None
            and _positive(row.get("discount_price")) is not None
        )
        if unit_only_unrepresentable:
            expected_master.clear()
            review_cases.append({"offer_id": row.get("id"), "rule_name": "unit_only_master_not_representable", "raw_text": raw})

        # The accepted historical arithmetic exceptions have no row-level
        # ledger here. Keep their master fields intact for evidence review.
        if arithmetic_only:
            expected_master = {}
        patch = _changed_fields(row, expected_master)
        projected_discount_fields = {
            field: patch.get(field, row.get(field))
            for field in ("discount_price", "discount_amount", "discount_percent")
        }
        if (
            patch
            and any(field in patch for field in projected_discount_fields)
            and all(_positive(value) is None for value in projected_discount_fields.values())
        ):
            patch = {}
            review_cases.append({"offer_id": row.get("id"), "rule_name": "master_discount_constraint", "raw_text": raw})
        if patch and row.get("updated_at"):
            master_updates.append({"id": row.get("id"), **patch, "updated_at": row.get("updated_at")})

        if len(items) == 1:
            item = items[0]
            item_expected: dict[str, Any] = {}
            if quantity is not None:
                item_expected["quantity"] = quantity
            elif total_price is not None or unit_price is not None:
                item_expected["quantity"] = None
            if multi_item or amount_only_is_unambiguous or _has_add_on(raw):
                item_expected["unit_price"] = None
            elif total_price is not None and quantity is not None:
                derived_unit = (total_price / quantity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                if derived_unit * quantity == total_price:
                    item_expected["unit_price"] = _unit(total_price / quantity)
                else:
                    item_expected["unit_price"] = None
                    review_cases.append({"offer_id": row.get("id"), "rule_name": "item_unit_not_representable_by_live_trigger", "raw_text": raw})
            elif total_price is not None and quantity is None:
                item_expected["unit_price"] = None
            elif unit_price is not None:
                if str(row.get("price_model") or "").strip().lower() == "from":
                    item_expected["unit_price"] = None
                    review_cases.append({"offer_id": row.get("id"), "rule_name": "starting_at_item_unit_not_representable", "raw_text": raw})
                else:
                    item_expected["unit_price"] = _unit(unit_price)
            item_patch = _changed_fields(item, item_expected)
            if item_patch and item.get("offer_item_id") is not None and item.get("updated_at"):
                item_updates.append({"offer_item_id": item["offer_item_id"], **item_patch, "updated_at": item["updated_at"]})
        elif len(items) > 1 and (multi_item or total_price is not None):
            for item in items:
                if item.get("unit_price") is not None and item.get("offer_item_id") is not None and item.get("updated_at"):
                    item_updates.append({"offer_item_id": item["offer_item_id"], "unit_price": None, "updated_at": item["updated_at"]})

        if multi_item and total_price is not None:
            review_cases.append({"offer_id": row.get("id"), "rule_name": "multi_item_total_allocation", "raw_text": raw})
        if explicit_total and unit_price is not None and quantity is not None:
            if abs((unit_price * quantity) - total_price) > Decimal("0.05"):
                review_cases.append({"offer_id": row.get("id"), "rule_name": "explicit_unit_total_rounding", "raw_text": raw})

    return SemanticRepairPlan(master_updates, item_updates, review_cases)
