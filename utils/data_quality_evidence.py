from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from utils.monitor_price_writer_plan import normalize_source_url, WriterError

# Thresholds ("minimum 40 units", "50+ units", "up to 4 treatments") are not delivered pack sizes.
_UNIT_WORDS = r"(?:units?|sessions?|syringes?|treatments?)"
_QUALIFIED_UNITS = re.compile(r"(?:minimum|min\.?|at least|up to|maximum|max\.?)\s+(?:of\s+)?\d+"
                              rf"|\d+\s*\+|\d+\s*{_UNIT_WORDS}\s*(?:or more|minimum|min\.?|\+)"
                              rf"|\d+[- ]?{_UNIT_WORDS}[- ]minimum", re.I)
_FIXED_UNITS = re.compile(r"\b([2-9]|[1-9]\d+)\s+(?:units|sessions|syringes|treatments)\b", re.I)

TABLES = {
    "promotions": ("clinic_promotions", "promotion_id"),
    "memberships": ("clinic_memberships", "plan_id"),
    "offers": ("promo_offer_master", "id"),
    "items": ("promo_offer_items", "offer_item_id"),
    "services": ("clinic_services", "service_id"),
}


def _stated_fixed_units(text: str) -> set[int]:
    thresholds = {int(re.search(r"\d+", m.group()).group()) for m in _QUALIFIED_UNITS.finditer(text)}
    return {int(n) for n in _FIXED_UNITS.findall(_QUALIFIED_UNITS.sub(" ", text))} - thresholds


def source_key(url: Any) -> str:
    try:
        return normalize_source_url(url)
    except WriterError:
        return ""


def current_capture(evidence: dict | None, url: str, at: datetime) -> bool:
    if not evidence or source_key(evidence.get("url", evidence.get("source_url"))) != source_key(url):
        return False
    try:
        stamp = datetime.fromisoformat(evidence["captured_at"].replace("Z", "+00:00"))
        return timedelta(0) <= at - stamp <= timedelta(hours=24) and evidence.get("verified") is True
    except (KeyError, ValueError, TypeError):
        return False


def gone_capture(evidence: dict | None, url: str, at: datetime) -> bool:
    """24 小时内的复查确认该地址返回 404/410。"""
    if not evidence or evidence.get("gone") is not True or source_key(evidence.get("url")) != source_key(url):
        return False
    stamp = datetime.fromisoformat(evidence["captured_at"].replace("Z", "+00:00"))
    return timedelta(0) <= at - stamp <= timedelta(hours=24)


def complete_capture(evidence: dict | None, url: str, at: datetime) -> bool:
    if not current_capture(evidence, url, at) or not evidence.get("complete"):
        return False
    views = evidence.get("viewports", [])
    return ({v.get("name") for v in views} == {"desktop", "tablet", "mobile"}
            and all(v.get("complete") is True for v in views)
            and evidence.get("image_review_complete") is True)


def _contains(quote: str, text: str) -> bool:
    return bool(quote.strip() and re.search(r"(?<!\w)" + re.escape(quote.strip()) + r"(?!\w)", text, re.I))


def _protected(snapshot: dict, table: str, row: dict, protections: dict) -> bool:
    refs = {(p.get("table"), p.get("id")) for p in protections.get("entries", [])}
    for key, name in (("offer_ids", "promo_offer_master"), ("item_ids", "promo_offer_items"),
                      ("promotion_ids", "clinic_promotions"), ("membership_ids", "clinic_memberships"),
                      ("service_ids", "clinic_services")):
        refs.update((name, value) for value in protections.get(key, []))
    pk = next(pk for name, pk in TABLES.values() if name == table)
    touched = {(table, row[pk])}
    offer_ids = set()
    if table == "promo_offer_master":
        offer_ids.add(row["id"])
    elif table == "promo_offer_items":
        offer_ids.add(row["offer_id"])
    elif table == "clinic_promotions":
        offer_ids.update(o["id"] for o in snapshot.get("offers", []) if o.get("promotion_id") == row[pk])
    elif table == "clinic_memberships":
        offer_ids.update(o["id"] for o in snapshot.get("offers", []) if o.get("membership_plan_id") == row[pk])
    elif table == "clinic_services":
        offer_ids.update(i["offer_id"] for i in snapshot.get("items", []) if i.get("service_id") == row[pk])
    for offer in snapshot.get("offers", []):
        if offer["id"] in offer_ids:
            touched.update({("promo_offer_master", offer["id"]),
                            ("clinic_promotions", offer.get("promotion_id")),
                            ("clinic_memberships", offer.get("membership_plan_id"))})
    touched.update(("promo_offer_items", i["offer_item_id"]) for i in snapshot.get("items", []) if i["offer_id"] in offer_ids)
    return bool(touched & refs)
