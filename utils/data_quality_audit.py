"""The issue-log rules applied to both historical rows and fresh captures."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from utils.monitor_price_writer import normalize_source_url, WriterError
from utils.offer_purchase_semantics import (canonical_offer_semantics, usable_unit_price,
                                           service_supported_by_source, is_generic_offer_scope)
from utils.offer_evidence_validator import _quantity_supported
from utils.offer_field_normalize import normalize_service_area
from utils.promo_offer_items_db import is_blanket_offer_text

# Thresholds ("minimum 40 units", "50+ units", "up to 4 treatments") are not delivered pack sizes.
_UNIT_WORDS = r"(?:units?|sessions?|syringes?|treatments?)"
_QUALIFIED_UNITS = re.compile(r"(?:minimum|min\.?|at least|up to|maximum|max\.?)\s+(?:of\s+)?\d+"
                              rf"|\d+\s*\+|\d+\s*{_UNIT_WORDS}\s*(?:or more|minimum|min\.?|\+)"
                              rf"|\d+[- ]?{_UNIT_WORDS}[- ]minimum", re.I)
_FIXED_UNITS = re.compile(r"\b([2-9]|[1-9]\d+)\s+(?:units|sessions|syringes|treatments)\b", re.I)


def _stated_fixed_units(text: str) -> set[int]:
    thresholds = {int(re.search(r"\d+", m.group()).group()) for m in _QUALIFIED_UNITS.finditer(text)}
    return {int(n) for n in _FIXED_UNITS.findall(_QUALIFIED_UNITS.sub(" ", text))} - thresholds

TABLES = {
    "promotions": ("clinic_promotions", "promotion_id"),
    "memberships": ("clinic_memberships", "plan_id"),
    "offers": ("promo_offer_master", "id"),
    "items": ("promo_offer_items", "offer_item_id"),
    "services": ("clinic_services", "service_id"),
}


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


def audit_snapshot(snapshot: dict, captures: dict | None = None, *, protections: dict | None = None,
                   at: datetime | None = None) -> dict:
    """Return findings and frozen row patches; never mutate a snapshot or infer prices."""
    at = at or datetime.now(timezone.utc)
    today = at.astimezone(ZoneInfo("America/Los_Angeles")).date()
    captures = {source_key(k): v for k, v in (captures or {}).items()}
    protections = protections or {}
    report = {"generated_at": at.isoformat(), "project_ref": "kdlpkjzcnbkjcvwsvlwn",
              "issues": [], "repairs": [], "rechecks": [], "price_output": []}
    promotions = {r["promotion_id"]: r for r in snapshot.get("promotions", [])}
    memberships = {r["plan_id"]: r for r in snapshot.get("memberships", [])}
    services = {r["service_id"]: r for r in snapshot.get("services", [])}
    items = defaultdict(list)
    for row in snapshot.get("items", []):
        items[row["offer_id"]].append(row)
    queued = {}

    def finding(rule, table, row, reason, *, values=None, parent=None, kind=None):
        pk = next(pk for name, pk in TABLES.values() if name == table)
        source = parent or row
        url = source.get("source_url", "")
        protected = _protected(snapshot, table, row, protections)
        issue = {"rule": rule, "table": table, "id": row[pk], "reason": reason,
                 "source_url": url, "business_id": source.get("business_id"),
                 "status": "protected" if protected else "ready" if values else "review"}
        report["issues"].append(issue)
        if values and not protected:
            patch = {k: v for k, v in values.items() if row.get(k) != v}
            if patch:
                report["repairs"].append({**issue, "values": patch, "before": dict(row),
                                           "source_kind": kind or "promotion"})
        elif url and not protected:
            key = (source_key(url), kind or "promotion", source.get("business_id"))
            entry = queued.setdefault(key, {"source_url": url, "source_kind": kind or "promotion",
                        "business_id": source.get("business_id"), "reasons": [], "status": "pending",
                        "requested_at": at.isoformat(), "promotion_ids": [], "membership_ids": [], "service_ids": []})
            if rule not in entry["reasons"]:
                entry["reasons"].append(rule)
            for field in ("promotion_id", "plan_id", "service_id"):
                target = {"promotion_id": "promotion_ids", "plan_id": "membership_ids", "service_id": "service_ids"}[field]
                if source.get(field) is not None and source[field] not in entry[target]:
                    entry[target].append(source[field])

    for promo in promotions.values():
        active_children = [o for o in snapshot.get("offers", []) if o.get("promotion_id") == promo["promotion_id"] and o.get("is_active")]
        url = promo.get("source_url", "")
        ev = captures.get(source_key(url))
        if not promo.get("is_active"):
            for child in active_children:
                finding("inactive_parent", "promo_offer_master", child, "inactive promotion has active child",
                        values={"is_active": False}, parent=promo)
            continue
        end = promo.get("campaign_end_date")
        try:
            expired = bool(end and date.fromisoformat(end[:10]) < today)
        except (TypeError, ValueError):
            expired = False
        if expired:
            # A fresh page must still support this exact campaign's deadline.
            verified_end = ev.get("campaign_end_date") if current_capture(ev, url, at) else None
            values = {"is_active": False, "is_offer_extracted": False} if verified_end == end and complete_capture(ev, url, at) else None
            if values:
                for child in active_children:
                    finding("expired_campaign", "promo_offer_master", child, "current page confirms campaign has ended",
                            values={"is_active": False}, parent=promo)
            finding("expired_campaign", "clinic_promotions", promo, "past deadline requires current page verification", values=values)
        content = promo.get("promotion_content")
        if complete_capture(ev, url, at) and isinstance(content, list):
            kept = [line for line in content if _contains(str(line), ev.get("text", ""))]
            if kept != content:
                finding("stale_content", "clinic_promotions", promo, "source reconciliation required; removal is planned by Monitor")
        try:
            captured = datetime.fromisoformat(ev["captured_at"].replace("Z", "+00:00")) if ev else None
        except (KeyError, ValueError, TypeError):
            captured = None
        if not captured or at - captured > timedelta(days=7):
            finding("stale_capture", "clinic_promotions", promo, "no successful source capture within seven days")
        if ev and ev.get("legacy_ocr_visible"):
            finding("ocr_visibility", "clinic_promotions", promo, "legacy OCR label is not current visual proof")
        if ev and ev.get("all_viewports_hidden"):
            finding("hidden_source", "clinic_promotions", promo, "all-viewports absence requires source reconciliation")

    for member in memberships.values():
        ev = captures.get(source_key(member.get("source_url")))
        if member.get("benefits") in (None, []):
            replacement = ev.get("memberships", {}).get(str(member["plan_id"])) if current_capture(ev, member.get("source_url", ""), at) else None
            values = None
            if replacement and replacement.get("verified") and replacement.get("benefits"):
                values = {"benefits": replacement["benefits"], "is_offer_extracted": False}
            finding("missing_benefits", "clinic_memberships", member, "unknown benefits must not mean verified empty",
                    values=values, kind="membership")
        if complete_capture(ev, member.get("source_url", ""), at) and member["plan_id"] in ev.get("absent_membership_ids", []):
            for offer in snapshot.get("offers", []):
                if offer.get("membership_plan_id") == member["plan_id"] and offer.get("is_active"):
                    finding("membership_removed", "promo_offer_master", offer, "complete page confirms membership removed",
                            values={"is_active": False}, parent=member, kind="membership")
            finding("membership_removed", "clinic_memberships", member, "no lifecycle column; preserve plan ID",
                    values={"is_offer_extracted": False}, kind="membership")

    for service in services.values():
        area = service.get("service_area")
        if not area:
            continue
        ev = captures.get(source_key(service.get("source_url")))
        suspicious = bool(re.search(r"\$|%|\b(?:off|free|save)\b", area, re.I))
        own = (ev or {}).get("services", {}).get(str(service["service_id"]), {})
        quote = str(own.get("quote") or "")
        # Full-page word absence alone cannot revoke an independently valid catalogue area.
        own_quote = (own.get("verified") is True and quote and _contains(quote, ev.get("text", ""))
                     and service_supported_by_source(service.get("service_name"), quote)) if ev else False
        normalized = normalize_service_area(area)
        supported = _contains(area, quote) or (normalized is not None and normalize_service_area(quote) == normalized)
        unsupported = complete_capture(ev, service.get("source_url", ""), at) and own_quote and not supported
        if suspicious or unsupported:
            finding("service_area_evidence", "clinic_services", service, "check service's own source, never associated offer absence",
                    values={"service_area": None} if complete_capture(ev, service.get("source_url", ""), at) and own_quote else None,
                    kind="service")
        elif not ev:
            finding("service_area_evidence", "clinic_services", service, "service area requires its own source capture", kind="service")

    duplicates = defaultdict(list)
    terms = {r["offer_id"]: r for r in snapshot.get("terms", [])}
    for offer in snapshot.get("offers", []):
        if offer.get("is_active") or items[offer["id"]]:
            continue
        created = offer.get("created_at")
        try:
            stale = bool(created and (at - datetime.fromisoformat(str(created).replace("Z", "+00:00"))) > timedelta(hours=24))
        except (TypeError, ValueError):
            stale = False
        if stale:
            parent = promotions.get(offer.get("promotion_id")) or memberships.get(offer.get("membership_plan_id")) or {}
            kind = "membership" if offer.get("membership_plan_id") else "promotion"
            finding("I_itemless_offer", "promo_offer_master", offer, "inactive offer shell without items",
                    parent=parent, kind=kind)
    for offer in snapshot.get("offers", []):
        if not offer.get("is_active"):
            continue
        parent = promotions.get(offer.get("promotion_id")) or memberships.get(offer.get("membership_plan_id")) or {}
        kind = "membership" if offer.get("membership_plan_id") else "promotion"
        raw = str(offer.get("offer_raw_text") or "")
        parts = items[offer["id"]]
        restriction = terms.get(offer["id"])
        terms_key = None if restriction is None else json.dumps({k: v for k, v in restriction.items()
            if k not in {"offer_id", "updated_at", "created_at"}}, sort_keys=True)
        blanket = is_blanket_offer_text(raw) or is_generic_offer_scope(raw)
        # Unlinked titles the blanket text never names are expansion artifacts, not composition.
        identity_parts = ([{**i, "item_name": None, "service_name": None} for i in parts]
                          if blanket and parts and all(i.get("service_id") is None
                              and not _contains(str(i.get("item_name") or ""), raw) for i in parts) else parts)
        duplicates[(offer.get("business_id"), offer.get("promotion_id"), offer.get("membership_plan_id"),
                    canonical_offer_semantics(offer, identity_parts), terms_key)].append(offer)
        if not parts:
            finding("missing_items", "promo_offer_master", offer, "active offer has no components", parent=parent, kind=kind)
        if re.search(r"\b(?:from|starting\s+at)\s*\$", raw, re.I) and offer.get("price_model") != "from":
            finding("F_from_price_dropped", "promo_offer_master", offer, "starting price stored as a definite total",
                    parent=parent, kind=kind)
        promo_lines = parent.get("promotion_content") if isinstance(parent.get("promotion_content"), list) else []
        blob = raw + " " + " ".join(str(c) for c in (offer.get("conditions") or []))
        if promo_lines:
            blob += " " + " ".join(str(line) for line in promo_lines)
        stated_units = set()
        if (offer.get("price_model") or "total") == "total":
            stated_units = _stated_fixed_units(" ; ".join([raw, *(str(c) for c in (offer.get("conditions") or []))]))
        missing_units = stated_units - {i.get("delivered_units") for i in parts}
        if offer.get("is_membership_required") and re.search(r"\bnon[- ]?members?\b", blob, re.I):
            regular = offer.get("regular_price")
            discount = offer.get("discount_price")
            stated = re.search(r"non[- ]?member[^$\d]{0,48}\$?\s*(\d[\d,]*(?:\.\d+)?)", blob, re.I)
            stated_price = float(stated.group(1).replace(",", "")) if stated else None
            compare = re.search(r"(?:regular(?:\s+price)?|compare)[^$\d]{0,12}\$?\s*(\d[\d,]*(?:\.\d+)?)", blob, re.I)
            compare_price = float(compare.group(1).replace(",", "")) if compare else None
            distinct_compare = (stated_price is not None and compare_price is not None
                                and abs(compare_price - stated_price) > 0.02)
            if distinct_compare:
                wrong_regular = regular is None or abs(float(regular) - compare_price) > 0.02
                detail = "compare-at price is regular_price; non-member sale stays its own offer"
            else:
                wrong_regular = stated_price is not None and regular is not None and abs(float(regular) - stated_price) > 0.02
                detail = "non-member tier must be regular_price on the member offer"
            if regular is None or (discount is not None and regular <= discount) or wrong_regular:
                finding("H_member_nonmember_regular", "promo_offer_master", offer, detail, parent=parent, kind=kind)
        from utils.offer_purchase_semantics import offer_has_unsplit_or
        if offer_has_unsplit_or(raw, is_package=offer.get("is_package"), item_count=len(parts)):
            finding("independent_options", "promo_offer_master", offer,
                    "classify purchasing alternatives before splitting", parent=parent, kind=kind)
        for item in parts:
            if missing_units and (item.get("delivered_units") is None or item.get("delivered_units") == 1):
                finding("G_units_in_text_not_delivered", "promo_offer_items", item,
                        "fixed pack size in text requires delivered_units from variant", parent=parent, kind=kind)
            report["price_output"].append({"offer_id": offer["id"], "offer_item_id": item["offer_item_id"],
                                          "price_model": offer.get("price_model"),
                                          "usable_unit_price": usable_unit_price(offer, item, raw)})
            if blanket and item.get("service_id") is not None:
                finding("blanket_service_mapping", "promo_offer_items", item, "unlinked scope required",
                        values={"service_id": None}, parent=parent, kind=kind)
            if blanket and (len(parts) != 1 or not _contains(str(item.get("item_name") or ""), raw)):
                finding("blanket_scope_components", "promo_offer_master", offer,
                        "generic scope must be reconciled to one evidenced unlinked item", parent=parent, kind=kind)
            if item.get("delivered_units") is not None and not _quantity_supported(item["delivered_units"], raw):
                finding("unsupported_quantity", "promo_offer_items", item, "fixed quantity has no delivery evidence", parent=parent, kind=kind)
            mapped = services.get(item.get("service_id"))
            if mapped and not blanket and not service_supported_by_source(mapped.get("service_name"), raw):
                finding("service_scope_evidence", "promo_offer_items", item, "explicit source service or controlled alias needs verification", parent=parent, kind=kind)
        if parent.get("is_offer_extracted") is True and any(i.get("service_id") is None for i in parts):
            table = "clinic_memberships" if kind == "membership" else "clinic_promotions"
            if not any(p["table"] == table and p["id"] == parent.get("plan_id", parent.get("promotion_id")) for p in report["repairs"]):
                finding("incomplete_mapping_flag", table, parent, "unlinked active item keeps source completion false",
                        values={"is_offer_extracted": False}, kind=kind)
    for group in duplicates.values():
        if len(group) < 2:
            continue
        # A protected member makes the entire proposed duplicate-group repair a review.
        protected = any(_protected(snapshot, "promo_offer_master", row, protections) for row in group)
        survivor = min(group, key=lambda r: r["id"])
        for row in group:
            if row["id"] == survivor["id"]:
                continue
            parent = promotions.get(row.get("promotion_id")) or memberships.get(row.get("membership_plan_id")) or {}
            finding("duplicate_offer", "promo_offer_master", row, f"complete commercial equivalent of {survivor['id']}",
                    values=None if protected else {"is_active": False}, parent=parent,
                    kind="membership" if row.get("membership_plan_id") else "promotion")
    # Detect possible baselines, never infer a regular price from another promotion.
    for offer in snapshot.get("offers", []):
        if offer.get("is_active") and offer.get("is_new_customer_required") and offer.get("regular_price") is None:
            siblings = [o for o in snapshot.get("offers", []) if o.get("is_active") and o["id"] != offer["id"]
                        and o.get("business_id") == offer.get("business_id") and o.get("promotion_id") == offer.get("promotion_id")
                        and o.get("membership_plan_id") == offer.get("membership_plan_id")
                        and o.get("is_new_customer_required") is False and o.get("discount_price") is not None
                        and {i.get("service_id") for i in items[o["id"]]} == {i.get("service_id") for i in items[offer["id"]]}]
            if siblings:
                finding("regular_price_baseline", "promo_offer_master", offer, "same-spec source-stated regular price required; do not borrow another sale",
                        parent=promotions.get(offer.get("promotion_id")) or memberships.get(offer.get("membership_plan_id")))
    report["rechecks"] = list(queued.values())
    report["counts"] = {key: len(report[key]) for key in ("issues", "repairs", "rechecks")}
    return report
