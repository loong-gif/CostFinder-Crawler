"""Source-backed purchase choices and comparable Offer semantics."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from typing import Any, Mapping

from utils.offer_bundle_semantics import (
    _BUY_GET, JOIN_PLUS, _apply_bundle_semantics, _item_label, _split_semicolon_masters)
from utils.service_price_guard import infer_explicit_per_unit_price

_OR_SPLIT = re.compile(r"\s*,\s*|\s+or\s+|\s+and\s+|\s*/\s*|\s+或\s+", re.I)
_CHOICE_INVALID = re.compile(
    r"\bor\s+(?:more|less|fewer|equal|any)\b|\b(?:minimum|at least|not|exclude|sign|email|text)\b",
    re.I,
)


def _join_branch(*parts: str) -> str:
    """拆分后的分支文本：只拼接含字母或数字的部分，句末的标点（如 "."）不单独成段。"""
    return " | ".join(p for p in parts if p and re.search(r"[A-Za-z0-9]", p))


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


def _composition_identity(master: Mapping[str, Any], items: list[dict]) -> tuple:
    source_text = re.sub(r"\$\s*\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s*%", " PRICE ", str(master.get("offer_raw_text") or ""))
    composition = tuple(sorted((
        (i.get("service_id"), _text(i.get("item_name") or i.get("service_name")),
         _number(i.get("delivered_units", i.get("quantity")))) for i in items
    ), key=repr))
    return composition, _text(source_text)


def offer_service_tier_identity(master: Mapping[str, Any], items: list[dict]) -> tuple:
    """Same purchasable service/composition; new vs returning is not a separate offer."""
    composition, source_text = _composition_identity(master, items)
    return (master.get("business_id"), master.get("promotion_id"), master.get("membership_plan_id"),
            master.get("is_package"), master.get("is_membership_required"),
            _conditions(master.get("conditions")), source_text, composition)


def offer_update_identity(master: Mapping[str, Any], items: list[dict]) -> tuple:
    """Compare source, composition and qualifications while allowing a price change."""
    composition, source_text = _composition_identity(master, items)
    return offer_service_tier_identity(master, items)


def new_customer_tier_merge_patch(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict | None:
    """Fold paired new/returning prices into one offer (new price on discount, returning on regular)."""
    ex_new = bool(existing.get("is_new_customer_required"))
    in_new = bool(incoming.get("is_new_customer_required"))
    if ex_new == in_new:
        return None
    patch: dict[str, Any] = {}
    if in_new and not ex_new:
        if incoming.get("discount_price") is not None:
            patch["discount_price"] = incoming.get("discount_price")
        if existing.get("discount_price") is not None:
            patch["regular_price"] = existing.get("discount_price")
        patch["is_new_customer_required"] = True
    else:
        if incoming.get("discount_price") is not None:
            patch["regular_price"] = incoming.get("discount_price")
    for field in ("discount_percent", "discount_amount", "price_model", "is_package", "is_membership_required"):
        if incoming.get(field) is not None and existing.get(field) != incoming.get(field):
            patch[field] = incoming.get(field)
    return patch if patch else None


def _valid_choice_parts(parts: list[str]) -> bool:
    if len(parts) < 2:
        return False
    return all(part and not re.search(r"\$|%|\b(?:off|free|when|with)\b", part, re.I) for part in parts)


def _split_choice_list(choices: str) -> list[str]:
    if _CHOICE_INVALID.search(choices):
        return []
    parts = [part.strip(" ,:") for part in _OR_SPLIT.split(choices) if part.strip(" ,:")]
    return parts if _valid_choice_parts(parts) else []


def _choice_parts(raw: str) -> tuple[list[str], str, str] | None:
    """Parse independent shared-price lists, not conjunctions in restrictions."""
    if _BUY_GET.search(raw):
        return None
    prefix, suffix, choices = "", "", ""
    percent = re.search(r"\b\d+(?:\.\d+)?\s*%\s*off\s+", raw, re.I)
    dollar_off = re.search(r"^\$\s*[\d,]+(?:\.\d+)?\s+off\s+", raw.strip(), re.I)
    trail_pct = re.search(r":\s*\d+(?:\.\d+)?\s*%\s*off\s*$", raw.strip(), re.I)
    if percent:
        start = percent.end()
        tail = re.search(r"[.;\n]|\s+(?:when|with|valid|minimum|excluding|only|cannot)\b", raw[start:], re.I)
        end = start + tail.start() if tail else len(raw)
        choices, prefix, suffix = raw[start:end].strip(), raw[:start].strip(), raw[end:].strip()
    elif dollar_off:
        start = dollar_off.end()
        tail = re.search(r"[.;\n]|\s+(?:when|with|valid|minimum|excluding|only|cannot)\b", raw[start:], re.I)
        end = start + tail.start() if tail else len(raw)
        choices, prefix, suffix = raw[start:end].strip(), raw[:start].strip(), raw[end:].strip()
    elif trail_pct:
        choices = raw[:trail_pct.start()].strip(" :")
        prefix, suffix = "", raw[trail_pct.start() + 1 :].strip()
    else:
        lead_price = re.match(r"^\$\s*[\d,]+(?:\.\d+)?(?:\s*/\s*(?:unit|area|syringe|session))?\s+(.+)$", raw.strip(), re.I)
        if lead_price:
            choices = lead_price.group(1).strip()
            prefix = re.match(r"^\$\s*[\d,]+(?:\.\d+)?(?:\s*/\s*(?:unit|area|syringe|session))?\s*", raw.strip(), re.I)
            prefix = prefix.group(0).strip() if prefix else ""
            suffix = ""
            tail = re.search(r"\s*[\(（].*[\)）]\s*$", choices)
            if tail:
                suffix = choices[tail.start() :].strip()
                choices = choices[: tail.start()].strip()
        else:
            if not re.search(r"\bor\b|或|/", raw, re.I):
                return None
            money = re.search(r"(?:优惠价|特价)?\s*\$\s*\d|(?:regular|reg\.?|原价)\s*[\$\d]", raw, re.I)
            if money is None:
                money = re.search(r"\$\s*\d", raw)
            if money is None:
                return None
            choices, prefix, suffix = raw[: money.start()].strip(" :"), "", raw[money.start() :].strip()
            choices = re.sub(r"^choose\s+(?:one\s*:\s*)?", "", choices, flags=re.I)
            choices = re.sub(r"(?:优惠价|特价)\s*$", "", choices, flags=re.I).strip()
            connector = re.search(r"\s+(?:for|at|only|just|now)\s*$", choices, re.I)
            if connector:
                suffix = f"{connector.group().strip()} {suffix}"
                choices = choices[: connector.start()].strip()
    if not re.search(r",|\bor\b|\band\b|或|/", choices, re.I):
        return None
    parts = _split_choice_list(choices)
    if not parts:
        return None
    return parts, prefix, suffix


def _areas_of_one_treatment(offer: Mapping[str, Any], parts: list[str]) -> bool:
    """唯一的 item 填了部位、且名称包含全部可选项（如 "Shoulder/Trap TOX"）：同一治疗的多个部位，不是可选的多个 offer。"""
    items = offer.get("items") or []
    if len(items) != 1 or not str(items[0].get("service_area") or "").strip():
        return False
    label = _text(_item_label(items[0]))
    return all(_text(part) in label for part in parts)


def offer_has_unsplit_or(raw: str, *, is_package: bool | None, item_count: int) -> bool:
    """True when active offer text/composition still looks like unsplit OR/AND."""
    if is_package:
        return False
    if item_count > 1:
        return True
    if _choice_parts(raw):
        return item_count <= 1
    return False


def _split_by_evidence_items(offer: dict[str, Any], raw: str) -> list[dict] | None:
    items = offer.get("items") or []
    if len(items) < 2 or offer.get("purchase_type") == "bundle" or offer.get("is_package") is True:
        return None
    if _BUY_GET.search(raw):
        return None
    branches: list[dict] = []
    for item in items:
        label = _item_label(item)
        if not label or not service_supported_by_source(label, raw):
            return None
        if item.get("unit_price") is not None and len(items) > 1:
            return None
        branches.append(deepcopy(item))
    split: list[dict] = []
    for item in branches:
        label = _item_label(item)
        branch = _join_branch(label, raw)
        split.append({**offer, "source_offer_raw_text": raw, "offer_raw_text": branch,
                      "purchase_type": "single", "is_package": False, "items": [item]})
    return split


def _split_or_embedded_in_item(offer: dict[str, Any], raw: str) -> list[dict] | None:
    items = offer.get("items") or []
    if len(items) != 1:
        return None
    label = _item_label(items[0])
    tail = re.search(r"([A-Z][A-Za-z0-9]+(?:\s+or\s+[A-Z][A-Za-z0-9]+)+)\s*$", label)
    if not tail:
        return None
    parts = _split_choice_list(tail.group(1))
    if not parts:
        return None
    split: list[dict] = []
    for part in parts:
        item = deepcopy(items[0])
        item["item_name"] = part
        if item.get("service_name") in (None, "", label):
            item["service_name"] = part
        split.append({**offer, "source_offer_raw_text": raw, "offer_raw_text": f"{part} | {raw}",
                      "purchase_type": "single", "is_package": False, "items": [item]})
    return split


def normalize_purchase_offers(offers: list[dict], evidence: str | Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    """Expand evidenced choices before persistence; leave uncertain input for review."""
    source = str(evidence.get("markdown") or "") if isinstance(evidence, Mapping) else str(evidence or "")
    output, reviews = [], []
    for index, original in enumerate(offers):
        offer = deepcopy(original)
        raw = str(offer.get("offer_raw_text") or "").strip()
        from utils.promo_offer_items_db import is_blanket_offer_text
        if is_blanket_offer_text(raw):
            from utils.service_recognition import scan_services
            # blanket offer 不对应具体服务：LLM 给出的名称是商品或服务大类（如 Skincare Products）时保留，
            # 名称指向枚举中的具体服务（如 Botox）时改用原文
            given = next((name for item in offer.get("items") or [] if isinstance(item, Mapping)
                          and (name := str(item.get("item_name") or "").strip()) and not scan_services(name)), "")
            offer.update(is_package=False, purchase_type="single", items=[{"item_name": given or raw, "service_name": None,
                "quantity": None, "unit_price": None, "unit_type": None, "service_area": None}])
            output.append(offer)
            if is_generic_offer_scope(raw):
                reviews.append({"offer_index": index, "offer_raw_text": raw, "reason": "generic_offer_scope"})
            continue
        if re.search(r"\b(?:from|starting\s+at|starts?\s+(?:at|from))\s+\$", raw, re.I):
            offer["price_model"] = "from"
        if is_generic_offer_scope(raw):
            offer.update(is_package=False, purchase_type="single", items=[{"item_name": raw, "service_name": None,
                "quantity": None, "unit_price": None, "unit_type": None, "service_area": None}])
            output.append(offer)
            reviews.append({"offer_index": index, "offer_raw_text": raw, "reason": "generic_offer_scope"})
            continue
        semicolon = _split_semicolon_masters(offer)
        if semicolon:
            expanded, semi_reviews = normalize_purchase_offers(semicolon, evidence)
            output.extend(expanded)
            reviews.extend({**review, "offer_index": index} for review in semi_reviews)
            continue
        per_unit = infer_explicit_per_unit_price(raw) is not None or offer.get("price_model") == "per_unit"
        if per_unit and re.search(JOIN_PLUS, raw):
            tail = re.sub(r"^.*?(?:(?:/\s*|\bper\s+)unit)\s+", "", raw, flags=re.I).strip(" :")
            brand_parts = [part.strip() for part in re.split(JOIN_PLUS, tail) if part.strip()] if tail else []
            if len(brand_parts) < 2:
                brand_parts = _split_choice_list(tail) if tail else []
            if len(brand_parts) >= 2:
                for part in brand_parts:
                    item = {"item_name": part, "service_name": part, "quantity": None, "unit_price": None,
                            "unit_type": "unit", "service_area": None}
                    branch = _join_branch(part, raw)
                    output.append({**offer, "source_offer_raw_text": raw, "offer_raw_text": branch,
                                   "purchase_type": "single", "is_package": False, "items": [item]})
                continue
            if len(offer.get("items") or []) >= 2:
                item_split = _split_by_evidence_items(offer, raw)
                if item_split:
                    output.extend(item_split)
                    continue
        choices = _choice_parts(raw)
        if choices and _areas_of_one_treatment(offer, choices[0]):
            choices = None
        evidenced = _text(raw) in _text(source) or not source.strip()
        if choices and evidenced:
            parts, prefix, suffix = choices
            for part in parts:
                candidates = [i for i in offer.get("items") or [] if _text(_item_label(i)) == _text(part)]
                item = deepcopy(candidates[0]) if len(candidates) == 1 else {
                    "item_name": part, "service_name": part, "quantity": None, "unit_price": None,
                    "unit_type": None, "service_area": None}
                branch = _join_branch(prefix, part, suffix)
                output.append({**offer, "source_offer_raw_text": raw, "offer_raw_text": branch,
                               "purchase_type": "single", "is_package": False, "items": [item]})
            continue
        item_split = _split_by_evidence_items(offer, raw)
        if item_split and (offer.get("purchase_type") == "independent_options" or choices):
            output.extend(item_split)
            continue
        if offer.get("purchase_type") == "independent_options" and len(offer.get("items") or []) == 1:
            # 只有一个 item、原文也没有并列选项时「互斥选项」标签不成立（LLM 有时把适用部位清单当成选项），按单项处理
            offer = {**offer, "purchase_type": "single"}
        if len(offer.get("items") or []) > 1 and re.search(
                r"\b(?:buy|purchase)\b.*\b(?:get|receive)\b.*\bfree\b", raw, re.I):
            offer.update(purchase_type="bundle", is_package=True)
        offer = _apply_bundle_semantics(offer, raw)
        embedded = _split_or_embedded_in_item(offer, raw)
        if embedded:
            output.extend(embedded)
            continue
        if offer.get("purchase_type") == "independent_options" and not choices and not item_split:
            fallback = _split_by_evidence_items(offer, raw)
            if fallback:
                output.extend(fallback)
                continue
            reviews.append({"offer_index": index, "offer_raw_text": raw, "reason": "unresolved_purchase_choices"})
            continue
        if len(offer.get("items") or []) > 1 and offer.get("purchase_type") != "bundle" and offer.get("is_package") is not True:
            reviews.append({"offer_index": index, "offer_raw_text": raw, "reason": "unresolved_purchase_composition"})
            continue
        output.append(offer)
    return output, reviews
