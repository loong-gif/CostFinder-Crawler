"""Pure, reviewable expansion of broad promo offers into service-level offers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from utils.offer_fingerprint import compute_offer_fingerprint
from utils.service_category_lookup import resolve_service_category

_GLOBAL = re.compile(r"\b(?:all|any)\s+(?:services?|treatments?|procedures?)\b|\b(?:site|store)[ -]?wide\b|\bfirst visit with us\b", re.I)
_CATEGORY = re.compile(r"\b(?:all|any)\s+(neurotox(?:in|ins?)|tox|fillers?|injectables?|facials?|laser(?: treatments?)?|chemical peels?|microneedling)\b", re.I)
_BUNDLE = re.compile(r"\b(?:package|bundle|includes?|plus|free|areas?|forehead|jawline|cheeks?|chin|lips?)\b|\b(?:vials?|syringes?)\b.*\+", re.I)
_NAMES = ("Botox", "Dysport", "Xeomin", "Jeuveau", "Daxxify", "Letybo", "Juvederm", "Restylane", "Sculptra", "Dermal Filler", "Kybella", "Skinvive")

@dataclass(frozen=True)
class ExpansionPlan:
    service_creates: list[dict[str, Any]]
    creates: list[dict[str, Any]]
    deactivate_offer_ids: list[int]
    skipped: list[dict[str, Any]]

def _source(offer: Mapping[str, Any]) -> str:
    promotion = offer.get("clinic_promotions")
    if isinstance(promotion, Mapping): return str(promotion.get("source_url") or "")
    if isinstance(promotion, list) and len(promotion) == 1 and isinstance(promotion[0], Mapping): return str(promotion[0].get("source_url") or "")
    return str(offer.get("source_url") or "")

def _category_for_service(service: Mapping[str, Any]) -> str | None:
    category, _, _ = resolve_service_category(str(service.get("service_name") or ""), service.get("service_category") or "", min_confidence="medium")
    return category

def _named(text: str) -> list[str]:
    return list(dict.fromkeys(name for name in _NAMES if re.search(rf"\b{re.escape(name)}\b", text, re.I)))

def _new_service(business_id: int, name: str, offer: Mapping[str, Any]) -> dict[str, Any]:
    category, _, _ = resolve_service_category(name, min_confidence="low")
    payload = {"business_id": business_id, "service_name": name, "service_name_raw": name,
               "service_category": "Neurotoxin" if category == "Neurotoxins" else "Filler" if category == "Fillers & Other Injectables" else "others",
               "unit_type": "unit" if re.search(r"\bunits?\b", str(offer.get("offer_raw_text") or ""), re.I) else "others"}
    source_url = _source(offer)
    if source_url:
        payload["source_url"] = source_url
    if re.search(r"\b(?:regular|reg\.?|was)\b", str(offer.get("offer_raw_text") or ""), re.I) and offer.get("regular_price"):
        payload["regular_price"] = float(offer["regular_price"])
    return payload

def _derived(offer: Mapping[str, Any], service: Mapping[str, Any], name: str) -> dict[str, Any]:
    master = {key: offer.get(key) for key in ("business_id", "promotion_id", "membership_plan_id", "discount_percent", "discount_amount", "is_membership_required", "offer_raw_text", "is_active", "is_new_customer_required", "price_model") if key in offer}
    raw_text = str(offer.get("offer_raw_text") or "")
    minimum = re.search(r"\b(\d+)\s*units?\s+minimum\b|\b(?:purchase|get)\s+(\d+)\s+(?:or\s+more\s+)?units?\b", raw_text, re.I)
    quantity = int(next(value for value in minimum.groups() if value)) if minimum else 1
    is_per_unit = master.get("price_model") == "per_unit"
    regular = service.get("regular_price")
    percent = offer.get("discount_percent")
    amount = offer.get("discount_amount")
    if regular is not None:
        regular = float(regular)
        if quantity > 1 and not is_per_unit:
            master["regular_price"] = regular * quantity
            master["discount_price"] = round(master["regular_price"] * (1 - float(percent) / 100), 2) if percent is not None else round(master["regular_price"] - float(amount), 2) if amount is not None else offer.get("discount_price")
        else:
            unit_discount = round(regular * (1 - float(percent) / 100), 2) if percent is not None else round(regular - float(amount), 2) if amount is not None else offer.get("discount_price")
            master["regular_price"] = regular * quantity if is_per_unit else regular
            master["discount_price"] = unit_discount * quantity if is_per_unit and unit_discount is not None else unit_discount
    else:
        # A source may publish an explicit sale price without publishing a
        # catalog/list price.  Keep that evidence so the master satisfies the
        # database's "has discount" invariant, but never invent a regular one.
        source_discount = offer.get("discount_price")
        master["regular_price"] = None; master["discount_price"] = source_discount * quantity if is_per_unit and source_discount is not None else source_discount
    if is_per_unit:
        master["price_model"] = "total"
        if amount is not None:
            master["discount_amount"] = float(amount) * quantity
    if master.get("regular_price") is not None and master.get("discount_price") is not None and master["discount_price"] > master["regular_price"]:
        # A catalog row can be stale or use a non-comparable unit.  Preserve the
        # explicitly published promotion price, but do not claim a false discount.
        master["regular_price"] = None
    unit = service.get("unit_type") or ""
    fingerprint_regular = master["regular_price"] / quantity if (is_per_unit or quantity > 1) and master["regular_price"] is not None else master["regular_price"]
    fingerprint_discount = master["discount_price"] / quantity if (is_per_unit or quantity > 1) and master["discount_price"] is not None else master["discount_price"]
    master["offer_fingerprint"] = compute_offer_fingerprint(source_url=_source(offer), service_name=name, unit_type=unit, regular_price=fingerprint_regular, discount_price=fingerprint_discount, offer_raw_text=str(offer.get("offer_raw_text") or ""))
    item_price = master["discount_price"] / quantity if (is_per_unit or quantity > 1) and master["discount_price"] is not None else master["discount_price"]
    return {"source_offer_id": offer["id"], "service_name": name, "master": master, "item": {"service_id": service.get("service_id"), "quantity": quantity, "unit_price": item_price}}

def build_expansion_plan(offers: Iterable[Mapping[str, Any]], services: Iterable[Mapping[str, Any]]) -> ExpansionPlan:
    services = tuple(services); creates=[]; service_creates_by_key={}; deactivate=[]; skipped=[]
    for offer in offers:
        if not offer.get("is_active") or not isinstance(offer.get("business_id"), int) or not isinstance(offer.get("id"), int): continue
        items = offer.get("promo_offer_items") or []
        if isinstance(items, list) and len(items) == 1 and items[0].get("service_id") is not None:
            # A prior expansion emits exactly this shape.  It is already a
            # service-level offer and must never become a new expansion source.
            continue
        text=str(offer.get("offer_raw_text") or ""); business_id=int(offer["business_id"]); existing=[s for s in services if s.get("business_id") == business_id and s.get("service_id") is not None]
        named=_named(text); targets=[]
        category_match=_CATEGORY.search(text)
        if _GLOBAL.search(text) and not named and not category_match: targets=existing
        elif category_match:
            category, _, _=resolve_service_category(category_match.group(1), min_confidence="low"); targets=[s for s in existing if _category_for_service(s) == category]
        elif len(named) >= 2:
            if _BUNDLE.search(text): skipped.append({"offer_id":offer["id"],"reason":"bundle_or_area"}); continue
            by_name={str(s.get("service_name")):s for s in existing}
            for name in named:
                targets.append(by_name.get(name) or {"business_id":business_id,"service_name":name,"regular_price":offer.get("regular_price"),"unit_type":"unit","_new":True})
        else: continue
        if not targets: skipped.append({"offer_id":offer["id"],"reason":"no_eligible_services"}); continue
        for target in targets:
            name=str(target["service_name"])
            if target.get("_new"):
                new=_new_service(business_id,name,offer); service_creates_by_key.setdefault((business_id, name), new); target={**target, **new, "service_id":None}
            creates.append(_derived(offer,target,name))
        deactivate.append(int(offer["id"]))
    return ExpansionPlan(service_creates=list(service_creates_by_key.values()), creates=creates, deactivate_offer_ids=deactivate, skipped=skipped)
