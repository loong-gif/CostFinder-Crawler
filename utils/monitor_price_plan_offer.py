from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import re

from utils.monitor_price_plan_common import (
    _PRICE_FIELDS, _Review, _id, _number, _rounded, _service_name_key, _text,
)
from utils.offer_evidence_validator import (
    _NEW_CUSTOMER_SIGNAL, stated_paid_and_delivered, validate_offer_evidence,
)
from utils.service_price_guard import infer_explicit_per_unit_price
from utils.promo_offer_items_db import is_blanket_offer_text
from utils.offer_purchase_semantics import service_supported_by_source


def _resolve_items(offer: dict, services: list[dict], business_id: int) -> list[dict]:
    raw_items = offer.get("items")
    if not isinstance(raw_items, list) or not raw_items or any(not isinstance(i, dict) for i in raw_items):
        raise _Review("missing_or_invalid_items")
    blanket = is_blanket_offer_text(offer.get("offer_raw_text"))
    items = []
    for item in raw_items:
        candidate_name = item.get("service_name")
        invalid_hint = bool(candidate_name and _text(candidate_name) != "others" and not service_supported_by_source(candidate_name, offer.get("source_offer_raw_text", offer.get("offer_raw_text"))))
        if invalid_hint:
            candidate_name = None
        if not candidate_name or _text(candidate_name) == "others":
            candidate_name = item.get("item_name")
        name = str(candidate_name or "").strip()
        if not name:
            raise _Review("missing_item_name")
        area = _text(item.get("service_area"))
        if blanket:
            service_id, stored_name = None, str(item.get("item_name") or name).strip()
        else:
            service_id = item.get("service_id")
            candidates = [s for s in services if s.get("business_id") == business_id]
            if service_id is not None:
                _id(service_id)
                candidates = [s for s in candidates if s.get("service_id") == service_id]
                candidates = [s for s in candidates if _service_name_key(s.get("service_name")) == _service_name_key(name)]
            else:
                candidates = [s for s in candidates if _service_name_key(s.get("service_name")) == _service_name_key(name)]
            candidates = [s for s in candidates if not invalid_hint and service_supported_by_source(s.get("service_name"), offer.get("source_offer_raw_text", offer.get("offer_raw_text")))]
            if area:
                candidates = [s for s in candidates if _text(s.get("service_area")) == area]
            if len(candidates) == 1:
                service_id = candidates[0]["service_id"]
            else:
                # Store the evidenced item without guessing a catalogue FK.
                service_id = None
            # item_name keeps the source title; the catalogue name is only the FK's label.
            stored_name = str(item.get("item_name") or name).strip()
        quantity = _number(item.get("delivered_units", item.get("quantity")), "delivered_units", positive=True)
        _number(item.get("unit_price"), "unit_price")
        if offer.get("quantity_kind", "fixed") != "fixed":
            quantity = None
        if quantity is not None and (quantity != quantity.to_integral_value() or quantity > 32767):
            raise _Review("unrepresentable_delivered_quantity")
        items.append({"item_name": stored_name, "service_id": service_id,
                      "delivered_units": int(quantity) if quantity is not None else None,
                      "unit_price": None})
    keys = [i["service_id"] if i["service_id"] is not None else _text(i["item_name"]) for i in items]
    if len(keys) != len(set(keys)):
        raise _Review("duplicate_offer_service")
    return items


def _validated_offer(raw: dict, markdown: str, items: list[dict]) -> tuple[dict | None, str | None]:
    candidate = deepcopy(raw)
    for field in _PRICE_FIELDS:
        value = _number(candidate.get(field), field)
        if field == "discount_percent" and value is not None and value > 100:
            raise _Review("invalid_discount_percent")
    conditions = candidate.get("conditions")
    if isinstance(conditions, list):
        candidate["conditions"] = [
            {"requirement": c, "evidence": c} if isinstance(c, str) else c for c in conditions
        ]
    candidate["items"] = []
    for original, resolved in zip(raw["items"], items):
        item = deepcopy(original)
        item["quantity"] = resolved["delivered_units"]
        # Explicit rates still need validation when a percent/amount-only
        # per-unit offer legitimately has no master discount_price.
        if raw.get("price_model") != "per_unit" or raw.get("discount_price") is not None:
            item.pop("unit_price", None)
        candidate["items"].append(item)
    if (raw.get("discount_price") is not None and not raw.get("is_package") and len(items) == 1
            and infer_explicit_per_unit_price(str(raw.get("offer_raw_text") or "")) is None
            and infer_explicit_per_unit_price(str(raw.get("offer_raw_text") or ""), prefer_first=True) is not None):
        return None, "ambiguous_unit_price_context"
    decision = validate_offer_evidence(candidate, markdown)
    if not decision.accepted:
        return None, decision.reason
    if any(issue.field in _PRICE_FIELDS for issue in decision.issues):
        return None, "unsupported_price_evidence"
    # Unsupported quantity/unit/area fields are written as NULL (the validator already cleared them). Still
    # quarantine an unevidenced item rate (price evidence) and a new-customer flag the page itself mentions.
    unsafe = [issue for issue in decision.issues if issue.field not in _PRICE_FIELDS and not issue.field.endswith(".service_name")]
    if any(issue.field.endswith(".unit_price") for issue in unsafe):
        return None, "unsupported_price_evidence"
    if any(issue.field == "is_new_customer_required" for issue in unsafe) and _NEW_CUSTOMER_SIGNAL.search(markdown):
        return None, "unsupported_scope_or_quantity"
    for issue in unsafe:
        cleared = re.fullmatch(r"items\[(\d+)\]\.quantity", issue.field)
        if cleared and int(cleared.group(1)) < len(items):
            items[int(cleared.group(1))]["delivered_units"] = None
    offer = decision.offer
    model = raw.get("price_model", "total")
    quantity_kind = raw.get("quantity_kind", "fixed")
    if model not in {"total", "per_unit", "from"} or quantity_kind not in {"fixed", "minimum", "variable"}:
        raise _Review("unsupported_price_or_quantity_model")
    package = raw.get("is_package", len(items) > 1)
    for flag in ("is_package", "is_membership_required", "is_new_customer_required"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise _Review(f"invalid_boolean:{flag}")
    if len(items) > 1 and not package:
        raise _Review("multiple_items_require_package")
    qty = _number(items[0]["delivered_units"], "delivered_units") if len(items) == 1 else None
    if model == "per_unit" and len(items) > 1:
        raise _Review("ambiguous_package_unit_price")
    prices = {field: offer.get(field) for field in _PRICE_FIELDS}
    rate_text = str(raw.get("offer_raw_text") or "")
    explicit_rate = infer_explicit_per_unit_price(rate_text)
    if (prices["discount_price"] is not None and not package and len(items) == 1 and explicit_rate is None
            and infer_explicit_per_unit_price(rate_text, prefer_first=True) is not None):
        return None, "ambiguous_unit_price_context"
    if model == "per_unit" and prices["discount_price"] is not None:
        if explicit_rate is None or abs(Decimal(str(explicit_rate)) - Decimal(str(prices["discount_price"]))) > Decimal(".0001"):
            return None, "per_unit_price_without_stated_rate"
    if model == "total" and prices["discount_price"] is not None and explicit_rate is not None:
        if abs(Decimal(str(explicit_rate))-Decimal(str(prices["discount_price"]))) <= Decimal(".0001"):
            model = "per_unit"
    if model == "from" and prices["discount_price"] is None and prices["regular_price"] is None:
        # Percent/amount-only discounts have no starting price to represent.
        model = "total"
    if model == "per_unit" and qty is not None:
        free_counts = stated_paid_and_delivered(str(raw.get("offer_raw_text") or ""))
        if free_counts and qty != free_counts[1]:
            return None, "paid_and_delivered_quantity_conflict"
        paid_qty = free_counts[0] if free_counts else qty
        for field in ("discount_price", "regular_price"):
            value = _number(prices[field], field)
            if value is not None:
                prices[field] = _rounded(value * (paid_qty if field == "discount_price" else qty))
        if prices["discount_amount"] is not None:
            prices["discount_amount"] = _rounded(_number(prices["discount_amount"], "discount_amount") * qty)
    regular = _number(prices["regular_price"], "regular_price")
    discount = _number(prices["discount_price"], "discount_price")
    if regular is not None and discount is not None and discount > regular:
        return None, "discount_exceeds_regular_price"
    if regular is not None and discount is not None:
        amount = _number(prices["discount_amount"], "discount_amount")
        percent = _number(prices["discount_percent"], "discount_percent")
        if (amount is not None and abs(regular-discount-amount) > Decimal(".011")
                or percent is not None and regular > 0 and
                abs((regular-discount)*100/regular-percent) >= Decimal("1")):
            return None, "contradictory_discount_evidence"
    bonus = stated_paid_and_delivered(rate_text)
    evidenced_bonus = bonus is not None and len(items) == 1 and qty == bonus[1]
    if not evidenced_bonus and not any(_number(prices[k], k) is not None and _number(prices[k], k) > 0 for k in _PRICE_FIELDS[1:]):
        return None, "no_supported_discount_field"
    for item_index, item in enumerate(items):
        delivered = _number(item["delivered_units"], "delivered_units")
        if not package and discount is not None:
            if delivered is not None:
                item["unit_price"] = _rounded(discount / delivered, "0.0001")
            else:
                item["unit_price"] = float(discount)
        elif not package and model == "per_unit":
            validated = offer["items"][item_index].get("unit_price")
            item["unit_price"] = float(_number(validated, "unit_price")) if validated is not None else None
    master = {**prices, "offer_raw_text": str(offer["offer_raw_text"]).strip(),
              "price_model": model, "is_package": package, "is_active": True,
              "is_membership_required": bool(offer.get("is_membership_required")),
              "is_new_customer_required": bool(offer.get("is_new_customer_required")),
              "conditions": offer["conditions"]}
    return master, None


def _term_values(master: dict) -> dict:
    conditions = master.get("conditions") or []
    if not isinstance(conditions, list) or any(not isinstance(c, str) for c in conditions):
        raise _Review("invalid_offer_conditions")
    text = " ".join([str(master.get("offer_raw_text") or ""), *conditions])
    unit_pattern = r"(units?|vials?|syringes?|sessions?|treatments?|areas?|items?)"
    minimum = re.search(r"\b(?:minimum|min\.?|at\s+least)\s+(\d+(?:\.\d+)?)\s*" + unit_pattern + r"\b", text, re.I)
    if minimum is None:
        minimum = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:\+|or\s+more)\s*" + unit_pattern + r"\b", text, re.I)
    if minimum is None:
        minimum = re.search(r"\b(\d+(?:\.\d+)?)\s*" + unit_pattern + r"\s*(?:minimum|or\s+more)\b", text, re.I)
    min_quantity = None
    min_unit = None
    if minimum:
        min_quantity = float(Decimal(minimum.group(1)))
        unit = minimum.group(2).lower().rstrip("s")
        min_unit = {"unit":"unit", "vial":"vial", "syringe":"syringe",
                    "session":"session", "treatment":"treatment", "area":"area", "item":"item"}.get(unit)
        if min_unit is None:
            raise _Review("unsupported_minimum_purchase_unit")
    one_per = bool(re.search(r"\b(?:one|1)\s+per\s+(?:patient|person|client)\b|"
                             r"\b(?:once|one time|1 time)\s+per\s+(?:patient|person|client)\b", text, re.I))
    return {"raw_terms":conditions, "min_quantity":min_quantity, "min_unit":min_unit,
            "is_one_per_patient":one_per,
            "is_new_customer_required":bool(master.get("is_new_customer_required")),
            "is_membership_required":bool(master.get("is_membership_required"))}
