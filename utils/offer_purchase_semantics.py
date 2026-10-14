"""Source-backed purchase choices and comparable Offer semantics."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Any, Mapping

from utils.service_price_guard import infer_explicit_per_unit_price


def _text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", text)).strip()


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number.normalize() if number.is_finite() else None
    except InvalidOperation:
        return None


def service_supported_by_source(service_name: Any, raw_text: Any) -> bool:
    """Require a named service or one of its controlled dictionary aliases."""
    name, source = _text(service_name), f" {_text(raw_text)} "
    if not name or name == "others":
        return False
    if f" {name} " in source:
        return True
    from utils.offer_extraction_llm import load_service_name_dictionary
    dictionary = load_service_name_dictionary()
    return any(_text(canonical) == name and f" {_text(alias)} " in source
               for alias, canonical in dictionary.get("aliases", {}).items())


def is_generic_offer_scope(raw_text: Any) -> bool:
    return bool(re.search(r"\b(?:favorite|selected|select)\s+(?:treatments?|products?|services?)\b",
                          str(raw_text or ""), re.I))


def usable_unit_price(master: Mapping[str, Any], item: Mapping[str, Any], raw_text: Any = None) -> float | None:
    """A trigger-derived total is not a usable rate without stated units."""
    if master.get("is_package") or master.get("price_model") == "from":
        return None
    raw = str(raw_text if raw_text is not None else master.get("offer_raw_text") or "")
    rate = infer_explicit_per_unit_price(raw)
    if rate is not None:
        return rate
    quantity = _number(item.get("delivered_units", item.get("quantity")))
    total = _number(master.get("discount_price"))
    if quantity is not None and quantity > 0 and total is not None:
        # Fixed counts must have survived the evidence gate before persistence.
        from utils.offer_evidence_validator import _quantity_supported
        if _quantity_supported(quantity, raw):
            return float((total / quantity).quantize(Decimal("0.0001")))
    return None


def _conditions(value: Any) -> tuple | None:
    if value is None:
        return None
    return tuple(sorted({_text(c.get("requirement") if isinstance(c, Mapping) else c) for c in value}))


def canonical_offer_semantics(master: Mapping[str, Any], items: list[dict]) -> tuple:
    """Full equality, distinct from identity used to preserve a changing price."""
    composition = tuple(sorted((
        (i.get("service_id"), _text(i.get("item_name") or i.get("service_name")),
         _number(i.get("delivered_units", i.get("quantity"))),
         _text(i.get("unit_type")) or None,
         _number(usable_unit_price(master, i))) for i in items
    ), key=repr))
    return (master.get("business_id"), master.get("promotion_id"), master.get("membership_plan_id"),
            master.get("price_model"), master.get("is_package"),
            *(_number(master.get(k)) for k in ("regular_price", "discount_price", "discount_amount", "discount_percent")),
            master.get("is_membership_required"), master.get("is_new_customer_required"),
            _conditions(master.get("conditions")), _text(master.get("offer_raw_text")), composition)


def offer_update_identity(master: Mapping[str, Any], items: list[dict]) -> tuple:
    """Compare source, composition and qualifications while allowing a price change."""
    source_text = re.sub(r"\$\s*\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s*%", " PRICE ", str(master.get("offer_raw_text") or ""))
    composition = tuple(sorted((
        (i.get("service_id"), _text(i.get("item_name") or i.get("service_name")),
         _number(i.get("delivered_units", i.get("quantity")))) for i in items
    ), key=repr))
    return (master.get("business_id"), master.get("promotion_id"), master.get("membership_plan_id"),
            master.get("is_package"), master.get("is_membership_required"), master.get("is_new_customer_required"),
            _conditions(master.get("conditions")), _text(source_text), composition)


def _choice_parts(raw: str) -> tuple[list[str], str, str] | None:
    """Parse independent shared-price lists, not conjunctions in restrictions."""
    if re.search(r"\b(?:buy|purchase)\b.*\b(?:get|receive)\b|\band/or\b", raw, re.I):
        return None
    percent = re.search(r"\b\d+(?:\.\d+)?\s*%\s*off\s+", raw, re.I)
    if percent:
        start = percent.end()
        tail = re.search(r"[.;\n]|\s+(?:when|with|valid|minimum|excluding|only|cannot)\b", raw[start:], re.I)
        end = start + tail.start() if tail else len(raw)
        choices = raw[start:end].strip()
        if not re.search(r",|\bor\b|\band\b", choices, re.I):
            return None
        prefix, suffix = raw[:start].strip(), raw[end:].strip()
    else:
        if not re.search(r"\bor\b", raw, re.I):
            return None
        money = re.search(r"\$\s*\d", raw)
        if money is None:
            return None
        choices, prefix, suffix = raw[:money.start()].strip(" :"), "", raw[money.start():].strip()
        choices = re.sub(r"^choose\s+(?:one\s*:\s*)?", "", choices, flags=re.I)
    if re.search(r"\bor\s+(?:more|less|fewer|equal|any)\b|\b(?:minimum|at least|not|exclude|sign|email|text)\b", choices, re.I):
        return None
    parts = [part.strip(" ,:") for part in re.split(r"\s*,\s*|\s+or\s+|\s+and\s+", choices, flags=re.I) if part.strip(" ,:")]
    if len(parts) < 2 or any(not p or re.search(r"\$|%|\b(?:off|free|when|with)\b", p, re.I) for p in parts):
        return None
    return parts, prefix, suffix


def normalize_purchase_offers(offers: list[dict], evidence: str | Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    """Expand evidenced choices before persistence; leave uncertain input for review."""
    source = str(evidence.get("markdown") or "") if isinstance(evidence, Mapping) else str(evidence or "")
    output, reviews = [], []
    for index, original in enumerate(offers):
        offer = deepcopy(original)
        raw = str(offer.get("offer_raw_text") or "").strip()
        from utils.promo_offer_items_db import is_blanket_offer_text
        if is_blanket_offer_text(raw):
            offer.update(is_package=False, purchase_type="single", items=[{"item_name": raw, "service_name": None,
                "quantity": None, "unit_price": None, "unit_type": None, "service_area": None}])
            output.append(offer)
            if is_generic_offer_scope(raw):
                reviews.append({"offer_index": index, "offer_raw_text": raw, "reason": "generic_offer_scope"})
            continue
        if re.search(r"\b(?:package|bundle|combo)\b", raw, re.I) and not re.search(r"\bor\b", raw, re.I):
            offer.update(purchase_type="bundle", is_package=True)
        if re.search(r"\b(?:from|starting\s+at|starts?\s+(?:at|from))\s+\$", raw, re.I):
            offer["price_model"] = "from"
        if is_generic_offer_scope(raw):
            offer.update(is_package=False, purchase_type="single", items=[{"item_name": raw, "service_name": None,
                "quantity": None, "unit_price": None, "unit_type": None, "service_area": None}])
            output.append(offer)
            reviews.append({"offer_index": index, "offer_raw_text": raw, "reason": "generic_offer_scope"})
            continue
        choices = _choice_parts(raw)
        if choices and _text(raw) in _text(source):
            parts, prefix, suffix = choices
            for part in parts:
                candidates = [i for i in offer.get("items") or [] if _text(i.get("item_name") or i.get("service_name")) == _text(part)]
                item = deepcopy(candidates[0]) if len(candidates) == 1 else {
                    "item_name": part, "service_name": part, "quantity": None, "unit_price": None,
                    "unit_type": None, "service_area": None}
                branch = " | ".join(p for p in (prefix, part, suffix) if p)
                output.append({**offer, "source_offer_raw_text": raw, "offer_raw_text": branch,
                               "purchase_type": "single", "is_package": False, "items": [item]})
            continue
        if len(offer.get("items") or []) > 1 and re.search(
                r"\b(?:buy|purchase)\b.*\b(?:get|receive)\b.*\bfree\b", raw, re.I):
            offer.update(purchase_type="bundle", is_package=True)
        if offer.get("purchase_type") == "independent_options" and not choices:
            reviews.append({"offer_index": index, "offer_raw_text": raw, "reason": "unresolved_purchase_choices"})
            continue
        if len(offer.get("items") or []) > 1 and offer.get("purchase_type") != "bundle" and offer.get("is_package") is not True:
            reviews.append({"offer_index": index, "offer_raw_text": raw, "reason": "unresolved_purchase_composition"})
            continue
        output.append(offer)
    return output, reviews
