from __future__ import annotations

from copy import deepcopy

from utils.monitor_price_plan_common import (
    ITEMS, OFFERS, PROMOTIONS, _Review, _raw_identity, _service_name_key, _text,
)
from utils.promo_offer_items_db import is_blanket_offer_text


def _promotion_content(existing: dict | None, quotes: list[str], complete: bool) -> list:
    if complete or existing is None:
        return quotes
    # Partial extraction cannot erase the unprocessed part of the source.
    content = deepcopy(existing.get("promotion_content") or [])
    for quote in quotes:
        matches = [index for index, old in enumerate(content)
                   if isinstance(old, str) and _raw_identity(old) == _raw_identity(quote)]
        if len(matches) == 1:
            content[matches[0]] = quote
        elif quote not in content:
            content.append(quote)
    return content


def _item_key(item: dict, blanket: bool = False) -> tuple:
    return (None if blanket else item.get("service_id"),
            "" if blanket else (_text(item.get("item_name")) if item.get("service_id") is None else ""))


def _identity(master: dict, items: list[dict]) -> tuple:
    blanket = is_blanket_offer_text(master.get("offer_raw_text"))
    composition = tuple(sorted((_item_key(i, blanket) for i in items), key=repr))
    conditions = master.get("conditions")
    return (composition, _raw_identity(master.get("offer_raw_text")) if blanket else None,
            bool(master.get("is_membership_required")),
            bool(master.get("is_new_customer_required")),
            tuple(sorted({_text(c) for c in conditions or []})))


def _offer_match_key(master: dict, items: list[dict]) -> tuple:
    """Same service/composition; new-patient vs returning is one offer."""
    blanket = is_blanket_offer_text(master.get("offer_raw_text"))
    composition = tuple(sorted((_item_key(i, blanket) for i in items), key=repr))
    return (composition, _raw_identity(master.get("offer_raw_text")) if blanket else None,
            bool(master.get("is_membership_required")))


def _catalog_items(items: list[dict], services: list[dict]) -> list[dict]:
    names = {s["service_id"]: s["service_name"] for s in services}
    return [{**i, "item_name": names.get(i.get("service_id"), i.get("item_name"))} for i in items]


def _nullable_item_match(old: dict, old_items: list[dict], new: dict, new_items: list[dict]) -> bool:
    """Fallback only for absent mappings, preserving unique existing IDs."""
    if len(old_items) != len(new_items) or not any(i.get("service_id") is None for i in [*old_items, *new_items]):
        return False
    if bool(old.get("is_membership_required")) != bool(new.get("is_membership_required")):
        return False
    return sorted(_service_name_key(i.get("item_name")) for i in old_items) == sorted(
        _service_name_key(i.get("item_name")) for i in new_items)


def _protected(protections: dict) -> dict[str, set[int]]:
    result = {PROMOTIONS: set(protections.get("promotion_ids", [])),
              OFFERS: set(protections.get("offer_ids", [])),
              ITEMS: set(protections.get("item_ids", []))}
    for entry in protections.get("entries", []):
        if isinstance(entry, dict) and entry.get("table") in result:
            result[entry["table"]].add(entry.get("id"))
    return result


def select_existing_promotion(promotions: list[dict], campaign: dict, source_promotion_id: int | None) -> dict | None:
    candidates = promotions
    has_campaign = any(campaign.values())
    if has_campaign:
        candidates = [p for p in candidates if all(
            ( _text(p.get(k)) == _text(v) if k == "promotion_title" else p.get(k) == v)
            for k, v in campaign.items() if v is not None)]
        if not candidates and not (campaign["campaign_start_date"] or campaign["campaign_end_date"]):
            candidates = [p for p in promotions if p.get("is_active") is True
                          and (source_promotion_id is None or p["promotion_id"] == source_promotion_id)]
    elif source_promotion_id is not None:
        candidates = [p for p in candidates if p["promotion_id"] == source_promotion_id]
    else:
        candidates = [p for p in candidates if p.get("is_active") is True]
    if len(candidates) > 1:
        raise _Review("ambiguous_source_campaign")
    existing_promotion = candidates[0] if candidates else None
    if existing_promotion is not None:
        if existing_promotion.get("is_active") is not True:
            raise _Review("historical_campaign_requires_review")
        # Current verified copy may omit a year/date. Null replaces
        # stale dates; an explicit expired date was rejected above.
    return existing_promotion


def match_existing_item(old_items: list[dict], desired: dict, blanket: bool, services: list[dict]) -> tuple[dict, dict | None]:
    matches = [i for i in old_items if _item_key(i, blanket) == _item_key(desired, blanket)]
    if not matches:
        matches = [i for i in old_items if (i.get("service_id") is None or desired["service_id"] is None)
                   and _service_name_key(_catalog_items([i], services)[0]["item_name"]) == _service_name_key(desired["item_name"])]
    if len(matches) > 1:
        raise _Review("ambiguous_item_match")
    if matches and desired["service_id"] is None and not blanket:
        old = matches[0]
        service = next((s for s in services if s["service_id"] == old.get("service_id")), None)
        if service and _service_name_key(service["service_name"]) == _service_name_key(desired["item_name"]):
            desired = {**desired, "service_id": service["service_id"]}
    return desired, matches[0] if matches else None


def check_protected_rows(operations: list[dict], protections: dict, offers: list[dict],
                         items: list[dict], existing_items: dict) -> None:
    protected = _protected(protections)
    offer_lookup = {o["id"]: o for o in offers}
    item_lookup = {i["offer_item_id"]: i for i in items}
    for op in operations:
        table, row_id = op["table"], op["id"]
        touches = {(table, row_id)}
        if table == OFFERS:
            touches.update((ITEMS, i["offer_item_id"]) for i in existing_items.get(row_id, []))
            parent_id = op["values"].get("promotion_id", offer_lookup.get(row_id, {}).get("promotion_id"))
            touches.add((PROMOTIONS, parent_id))
        if table == ITEMS:
            parent_id = op["values"].get("offer_id", item_lookup.get(row_id, {}).get("offer_id"))
            touches.add((OFFERS, parent_id))
            touches.add((PROMOTIONS, offer_lookup.get(parent_id, {}).get("promotion_id")))
        if table == PROMOTIONS:
            children = ([o for o in offers if o["promotion_id"] == row_id]
                        if set(op["values"]) - {"is_offer_extracted"} else [])
            touches.update((OFFERS, o["id"]) for o in children)
            touches.update((ITEMS, i["offer_item_id"]) for o in children for i in existing_items[o["id"]])
        if any(touched_table in protected and touched_id in protected[touched_table]
               for touched_table, touched_id in touches):
            raise _Review("protected_row_or_cascade")
