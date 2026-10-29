#!/usr/bin/env python3
"""Dry-run/apply the source-backed Offer repairs selected on 2026-09-18.

The default mode is read-only.  Only the explicit source-backed repairs in the
execution plan are eligible for ``--apply``; unresolved source coverage remains
in the generated review queue.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.offer_plan_20260918 import (  # noqa: E402
    extract_explicit_terms,
    should_clear_per_unit_quantity,
    split_offer_fingerprint,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402


MASTER_SELECT = (
    "id,business_id,promotion_id,membership_plan_id,is_active,is_package,price_model,"
    "regular_price,discount_price,discount_percent,discount_amount,is_membership_required,"
    "is_new_customer_required,offer_fingerprint,offer_raw_text,conditions,updated_at,"
    "promo_offer_items(offer_item_id,item_name,service_id,delivered_units,unit_price,updated_at)"
)
SERVICE_SELECT = "service_id,business_id,service_name,regular_price,unit_type,service_area,source_url"
PROMOTION_SELECT = "promotion_id,business_id,source_url,is_active,updated_at"
MEMBERSHIP_SELECT = "plan_id,business_id,is_offer_extracted,updated_at"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_all(client: SupabaseRestClient, table: str, select: str, *, order: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(table, select, limit=1000, offset=offset, order=order)
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += len(page)


def load_client() -> SupabaseRestClient:
    load_dotenv(ROOT / ".env")
    url = os.environ.get("SUPABASE_URL")
    if not url:
        raise RuntimeError("缺少 SUPABASE_URL")
    return SupabaseRestClient(url, get_supabase_writer_key())


def item_list(offer: dict[str, Any]) -> list[dict[str, Any]]:
    value = offer.get("promo_offer_items") or []
    return value if isinstance(value, list) else [value]


def normalized_conditions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def has_term(conditions: list[str], term: str) -> bool:
    folded = re.sub(r"\s+", " ", term.casefold()).strip()
    if any(re.sub(r"\s+", " ", item.casefold()).strip() == folded for item in conditions):
        return True
    minimum = re.fullmatch(r"minimum\s+(\d+(?:\.\d+)?)\s+(\w+)", folded)
    if minimum:
        number, unit = minimum.groups()
        return any(
            re.search(rf"\b{re.escape(number)}\b", item, re.IGNORECASE)
            and re.search(rf"\b{re.escape(unit.rstrip('s'))}s?\b", item, re.IGNORECASE)
            and re.search(r"\+|minimum|at\s+least|or\s+more|required", item, re.IGNORECASE)
            for item in conditions
        )
    return False


def source_key(offer: dict[str, Any]) -> str:
    if offer.get("membership_plan_id") is not None:
        return f"m:{offer['membership_plan_id']}"
    return f"p:{offer['promotion_id']}"


def service_for(services: list[dict[str, Any]], business_id: Any, service_name: str) -> dict[str, Any] | None:
    for service in services:
        if service.get("business_id") == business_id and service.get("service_name") == service_name:
            return service
    return None


def customer_restriction(raw: Any) -> bool:
    return bool(
        re.search(
            r"\b(?:new\s+(?:patients?|clients?|customers?)|first[- ]time|first\s+visit)\b",
            str(raw or ""),
            re.IGNORECASE,
        )
    )


def load_state(client: SupabaseRestClient) -> dict[str, list[dict[str, Any]]]:
    return {
        "offers": fetch_all(client, "promo_offer_master", MASTER_SELECT, order="id.asc"),
        "services": fetch_all(client, "clinic_services", SERVICE_SELECT, order="service_id.asc"),
        "promotions": fetch_all(client, "clinic_promotions", PROMOTION_SELECT, order="promotion_id.asc"),
        "memberships": fetch_all(client, "clinic_memberships", MEMBERSHIP_SELECT, order="plan_id.asc"),
    }


def add_master_update(updates: dict[int, dict[str, Any]], offer: dict[str, Any], **fields: Any) -> None:
    fields = {key: value for key, value in fields.items() if offer.get(key) != value}
    if not fields:
        return
    operation = updates.setdefault(int(offer["id"]), {"id": int(offer["id"]), "updated_at": offer.get("updated_at"), "fields": {}})
    operation["fields"].update(fields)


def add_item_update(updates: dict[int, dict[str, Any]], item: dict[str, Any], **fields: Any) -> None:
    if not fields:
        return
    operation = updates.setdefault(int(item["offer_item_id"]), {"offer_item_id": int(item["offer_item_id"]), "updated_at": item.get("updated_at"), "fields": {}})
    operation["fields"].update(fields)


def build_plan(state: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    offers = state["offers"]
    services = state["services"]
    by_id = {int(row["id"]): row for row in offers}
    master_updates: dict[int, dict[str, Any]] = {}
    item_updates: dict[int, dict[str, Any]] = {}
    service_inserts: list[dict[str, Any]] = []
    new_offers: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []

    def target(offer_id: int, marker: str) -> dict[str, Any] | None:
        offer = by_id.get(offer_id)
        if not offer or marker.casefold() not in str(offer.get("offer_raw_text") or "").casefold():
            review.append({"offer_id": offer_id, "reason": "expected_source_text_not_found", "marker": marker})
            return None
        return offer

    offer_9797 = target(9797, "80 Units of Tox")
    if offer_9797 and offer_9797.get("is_package") is not True:
        add_master_update(master_updates, offer_9797, is_package=True)

    offer_9136 = target(9136, "ALL TOXINS")
    if offer_9136:
        if by_id.get(9518, {}).get("is_active"):
            add_master_update(master_updates, by_id[9518], is_active=False)
        sibling_ids = {"Jeuveau": 10132, "Dysport": 10133, "Xeomin": 10134, "Daxxify": 10135}
        for service_name, sibling_id in sibling_ids.items():
            sibling = by_id.get(sibling_id)
            if sibling is None:
                review.append({"offer_id": 9136, "reason": "missing_existing_atomic_sibling", "service_name": service_name})
                continue
            add_master_update(
                master_updates,
                sibling,
                is_active=True,
                is_package=False,
                conditions=["Applicable membership tier: Platinum"],
            )

    offer_1703 = target(1703, "Dermal Fillers and Sculptra")
    if offer_1703:
        has_dermal_filler = any(
            str(item.get("item_name") or "").casefold() == "dermal filler"
            for item in item_list(offer_1703)
        )
        dermal_filler = service_for(services, offer_1703["business_id"], "Dermal Filler")
        if not has_dermal_filler and dermal_filler:
            new_offers.append(
                {
                    "dedupe_key": "m:238:Dermal Filler",
                    "business_id": offer_1703["business_id"],
                    "promotion_id": None,
                    "membership_plan_id": offer_1703["membership_plan_id"],
                    "is_package": False,
                    "price_model": offer_1703["price_model"],
                    "regular_price": None,
                    "discount_price": None,
                    "discount_percent": 10.0,
                    "discount_amount": None,
                    "is_membership_required": True,
                    "is_new_customer_required": False,
                    "offer_raw_text": offer_1703["offer_raw_text"],
                    "conditions": normalized_conditions(offer_1703.get("conditions")),
                    "service_name": "Dermal Filler",
                    "service_id": dermal_filler["service_id"],
                }
            )
        elif not dermal_filler:
            review.append({"offer_id": 1703, "reason": "missing_dermal_filler_service"})

    def add_membership_sibling(
        source_offer_id: int,
        service_name: str,
        raw_text: str,
        discount_percent: float,
        dedupe_key: str,
    ) -> None:
        source_offer = by_id.get(source_offer_id)
        if not source_offer:
            review.append({"offer_id": source_offer_id, "reason": "source_offer_missing", "service_name": service_name})
            return
        service = service_for(services, source_offer["business_id"], service_name)
        if not service:
            review.append({"offer_id": source_offer_id, "reason": "missing_service", "service_name": service_name})
            return
        new_offers.append(
            {
                "dedupe_key": dedupe_key,
                "business_id": source_offer["business_id"],
                "promotion_id": None,
                "membership_plan_id": source_offer["membership_plan_id"],
                "is_package": False,
                "price_model": "total",
                "regular_price": None,
                "discount_price": None,
                "discount_percent": discount_percent,
                "discount_amount": None,
                "is_membership_required": True,
                "is_new_customer_required": False,
                "offer_raw_text": raw_text,
                "conditions": normalized_conditions(source_offer.get("conditions")),
                "service_name": service_name,
                "service_id": service["service_id"],
            }
        )

    for service_name in ("Microneedling", "IV Therapy"):
        add_membership_sibling(
            9025,
            service_name,
            "15% off Laser treatments, Microneedling, Skincare, and IV Therapy",
            15.0,
            f"m:482:{service_name}",
        )
    add_membership_sibling(
        7936,
        "Microneedling",
        "10% OFF FILLER, MICRONEEDLING, LASER HAIR REMOVAL, AND FACIALS",
        10.0,
        "m:496:Microneedling",
    )
    add_membership_sibling(
        1703,
        "Microneedling",
        "10% off all laser, microneedling, and treatment packages (20% total)",
        10.0,
        "m:238:Microneedling",
    )

    # Promotion 3526 explicitly publishes two membership Botox unit prices;
    # both map to the already-existing Botox service for business 3071.
    cosmetiq_biz = 3071
    cosmetiq_service = service_for(services, cosmetiq_biz, "Botox")
    if not cosmetiq_service:
        review.append({"promotion_id": 3526, "reason": "missing_botox_service"})
    else:
        for tier, unit_price in (("Elite", 10.0), ("Glow", 11.0)):
            new_offers.append(
                {
                    "dedupe_key": f"p:3526:Botox:{tier}",
                    "business_id": cosmetiq_biz,
                    "promotion_id": 3526,
                    "membership_plan_id": None,
                    "is_package": False,
                    "price_model": "per_unit",
                    "regular_price": cosmetiq_service.get("regular_price"),
                    "discount_price": unit_price,
                    "discount_percent": None,
                    "discount_amount": None,
                    "is_membership_required": True,
                    "is_new_customer_required": False,
                    "offer_raw_text": f"Botox from ${unit_price:g} per unit; {tier} member price ${unit_price:g}",
                    "conditions": [f"{tier} member"],
                    "service_name": "Botox",
                    "service_id": cosmetiq_service["service_id"],
                    "unit_price": unit_price,
                }
            )

    review.extend(
        {
            "promotion_id": promotion_id,
            "reason": "source_names_service_but_live_service_category_required_and_source_category_missing",
            "service_name": service_name,
        }
        for promotion_id, service_name in (
            (685, "Emsella Chair"),
            (685, "Exomind"),
            (685, "Emsculpt Neo"),
            (3469, "Mesotherapy Glass Skin Facial + Dermapen (PA/RN)"),
            (3469, "Microlipo Neck Liposuction without General Anesthesia"),
        )
    )

    offer_1517 = target(1517, "40 or more units")
    if offer_1517:
        add_master_update(
            master_updates,
            offer_1517,
            conditions=["minimum 40 units"],
        )
        for item in item_list(offer_1517):
            add_item_update(item_updates, item, delivered_units=None)
        dysport = service_for(services, offer_1517.get("business_id"), "Dysport")
        if not dysport:
            review.append({"offer_id": 1517, "reason": "missing_dysport_service"})
        else:
            new_offers.append(
                {
                    "dedupe_key": "p:909:Dysport",
                    "business_id": offer_1517["business_id"],
                    "promotion_id": offer_1517["promotion_id"],
                    "membership_plan_id": None,
                    "is_package": False,
                    "price_model": "total",
                    "regular_price": offer_1517.get("regular_price"),
                    "discount_price": None,
                    "discount_percent": None,
                    "discount_amount": 100.0,
                    "is_membership_required": False,
                    "is_new_customer_required": True,
                    "offer_raw_text": offer_1517["offer_raw_text"],
                    "conditions": ["minimum 40 units"],
                    "service_name": "Dysport",
                    "service_id": dysport["service_id"],
                }
            )

    offer_10033 = target(10033, "Juv")
    if offer_10033:
        claim_rows = []
        try:
            claim_rows = fetch_all(load_client(), "claims", "id,deal_id", order="id.asc")
        except Exception:
            review.append({"offer_id": 10033, "reason": "claim_reference_check_unavailable"})
        if any(str(row.get("deal_id")) == "10033" for row in claim_rows):
            review.append({"offer_id": 10033, "reason": "existing_claim_reference_blocks_deactivation"})
        else:
            add_master_update(master_updates, offer_10033, is_active=False)
            for name in ("Juvéderm", "Restylane"):
                service = service_for(services, offer_10033.get("business_id"), name)
                if not service:
                    service_inserts.append(
                        {
                            "business_id": offer_10033["business_id"],
                            "service_name": name,
                            "source_url": next(
                                (row.get("source_url") for row in state["promotions"] if row.get("promotion_id") == offer_10033.get("promotion_id")),
                                None,
                            ),
                        }
                    )
                    service = {"service_id": None, "business_id": offer_10033["business_id"], "service_name": name}
                new_offers.append(
                    {
                        "dedupe_key": f"p:{offer_10033['promotion_id']}:{name}",
                        "business_id": offer_10033["business_id"],
                        "promotion_id": offer_10033["promotion_id"],
                        "membership_plan_id": None,
                        "is_package": False,
                        "price_model": "total",
                        "regular_price": None,
                        "discount_price": None,
                        "discount_percent": 15.0,
                        "discount_amount": None,
                        "is_membership_required": False,
                        "is_new_customer_required": False,
                        "offer_raw_text": offer_10033["offer_raw_text"],
                        "conditions": ["promo code 15OFFFILLER"],
                        "service_name": name,
                        "service_id": service["service_id"],
                    }
                )

    for offer_id, service_name, unit_price, discount_price, percent in (
        (9443, "Botox", 12.32, None, None),
        (9444, None, None, None, None),
    ):
        offer = by_id.get(offer_id)
        if not offer:
            review.append({"offer_id": offer_id, "reason": "target_offer_missing"})
            continue
        if offer_id == 9443:
            botox = service_for(services, offer["business_id"], "Botox")
            if botox:
                for item in item_list(offer):
                    add_item_update(item_updates, item, item_name="Botox", service_id=botox["service_id"], unit_price=unit_price)
            else:
                review.append({"offer_id": offer_id, "reason": "missing_botox_service"})
            dysport = service_for(services, offer["business_id"], "Dysport")
            if dysport:
                new_offers.append(
                    {
                        "dedupe_key": "m:480:Dysport",
                        "business_id": offer["business_id"],
                        "promotion_id": None,
                        "membership_plan_id": offer["membership_plan_id"],
                        "is_package": False,
                        "price_model": "per_unit",
                        "regular_price": None,
                        "discount_price": 4.10,
                        "discount_percent": 12.0,
                        "discount_amount": None,
                        "is_membership_required": True,
                        "is_new_customer_required": False,
                        "offer_raw_text": offer["offer_raw_text"],
                        "conditions": normalized_conditions(offer.get("conditions")),
                        "service_name": "Dysport",
                        "service_id": dysport["service_id"],
                    }
                )
        else:
            dysport = service_for(services, offer["business_id"], "Dysport")
            if dysport:
                new_offers.append(
                    {
                        "dedupe_key": "m:481:Dysport",
                        "business_id": offer["business_id"],
                        "promotion_id": None,
                        "membership_plan_id": offer["membership_plan_id"],
                        "is_package": False,
                        "price_model": "per_unit",
                        "regular_price": None,
                        "discount_price": 3.72,
                        "discount_percent": 20.0,
                        "discount_amount": None,
                        "is_membership_required": True,
                        "is_new_customer_required": False,
                        "offer_raw_text": offer["offer_raw_text"],
                        "conditions": normalized_conditions(offer.get("conditions")),
                        "service_name": "Dysport",
                        "service_id": dysport["service_id"],
                    }
                )

    for offer in offers:
        if not offer.get("is_active"):
            continue
        if offer.get("is_membership_required") is not True and offer.get("membership_plan_id") is not None:
            add_master_update(master_updates, offer, is_membership_required=True)
        terms = extract_explicit_terms(offer.get("offer_raw_text"))
        conditions = normalized_conditions(offer.get("conditions"))
        missing_terms = [term for term in terms if not has_term(conditions, term)]
        if missing_terms:
            add_master_update(master_updates, offer, conditions=conditions + missing_terms)
        if customer_restriction(offer.get("offer_raw_text")) and offer.get("is_new_customer_required") is not True:
            add_master_update(master_updates, offer, is_new_customer_required=True)
        for item in item_list(offer):
            if (
                offer.get("price_model") == "per_unit"
                and item.get("delivered_units") == 1
                and should_clear_per_unit_quantity(offer.get("offer_raw_text"))
            ):
                add_item_update(item_updates, item, delivered_units=None)
            if (
                offer.get("is_package") is False
                and item.get("delivered_units") == 1
                and re.search(r"\b(?:minimum|at\s+least|or\s+more|\d+\s*\+)\b", str(offer.get("offer_raw_text") or ""), re.IGNORECASE)
            ):
                add_item_update(item_updates, item, delivered_units=None)
            if (
                offer.get("is_package") is False
                and item.get("delivered_units") == 1
                and re.search(r"\bany\b", str(offer.get("offer_raw_text") or ""), re.IGNORECASE)
                and not re.search(r"\b\d+(?:\.\d+)?\s+(?:units?|sessions?|treatments?|syringes?|vials?)\b", str(offer.get("offer_raw_text") or ""), re.IGNORECASE)
            ):
                add_item_update(item_updates, item, delivered_units=None)

    memberships = {int(row["plan_id"]): row for row in state["memberships"]}
    active_by_plan = {int(offer["membership_plan_id"]) for offer in offers if offer.get("is_active") and offer.get("membership_plan_id") is not None}
    membership_status_updates = []
    for plan_id, plan in memberships.items():
        if plan.get("is_offer_extracted") is True and plan_id not in active_by_plan:
            membership_status_updates.append(
                {"plan_id": plan_id, "updated_at": plan.get("updated_at"), "fields": {"is_offer_extracted": False}, "reason": "treatment_benefit_without_active_offer" if plan_id == 368 else "no_active_offer_review"}
            )

    return {
        "generated_at": now(),
        "mode": "dry_run",
        "source_schema": "conditions_text_array_for_terms; no promo_offer_terms table in live schema",
        "summary": {
            "master_updates": len(master_updates),
            "item_updates": len(item_updates),
            "service_inserts": len(service_inserts),
            "new_offer_specs": len(new_offers),
            "membership_status_updates": len(membership_status_updates),
            "review_items": len(review),
        },
        "master_updates": list(master_updates.values()),
        "item_updates": list(item_updates.values()),
        "service_inserts": service_inserts,
        "new_offer_specs": new_offers,
        "membership_status_updates": membership_status_updates,
        "review": review,
    }


def _apply_update(client: SupabaseRestClient, table: str, key: str, operation: dict[str, Any]) -> dict[str, Any]:
    filters = {key: f"eq.{operation[key]}"}
    if operation.get("updated_at"):
        filters["updated_at"] = f"eq.{operation['updated_at']}"
    payload = {**operation.get("fields", {}), "updated_at": now()}
    returned = client.update_row(table, filters, payload)
    return {"table": table, "id": operation[key], "updated": bool(returned), "returned": returned[:1]}


def apply_plan(client: SupabaseRestClient, plan: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"master_updates": [], "item_updates": [], "services": [], "new_offers": [], "membership_status_updates": [], "errors": []}
    services = fetch_all(client, "clinic_services", SERVICE_SELECT, order="service_id.asc")
    for spec in plan["service_inserts"]:
        existing = service_for(services, spec["business_id"], spec["service_name"])
        if existing:
            spec["service_id"] = existing["service_id"]
            continue
        try:
            inserted = client.insert_rows(
                "clinic_services",
                [{k: v for k, v in spec.items() if k != "service_id"} | {"updated_at": now()}],
            )
            row = inserted[0]
            spec["service_id"] = row["service_id"]
            services.append(row)
            result["services"].append(row)
        except Exception as exc:
            result["errors"].append({"operation": "service_insert", "spec": spec, "error": str(exc)})
    for operation in plan["master_updates"]:
        try:
            result["master_updates"].append(_apply_update(client, "promo_offer_master", "id", operation))
        except Exception as exc:
            result["errors"].append({"operation": "master_update", "id": operation["id"], "error": str(exc)})
    for operation in plan["item_updates"]:
        try:
            result["item_updates"].append(_apply_update(client, "promo_offer_items", "offer_item_id", operation))
        except Exception as exc:
            result["errors"].append({"operation": "item_update", "id": operation["offer_item_id"], "error": str(exc)})
    current = fetch_all(client, "promo_offer_master", MASTER_SELECT, order="id.asc")
    existing_keys = {
        (source_key(offer), service.get("service_name"))
        for offer in current
        if offer.get("is_active")
        for service in [{"service_name": item.get("item_name")} for item in item_list(offer)]
        if item_list(offer)
    }
    for spec in plan["new_offer_specs"]:
        if (source_key(spec), spec["service_name"]) in existing_keys:
            continue
        service = service_for(services, spec["business_id"], spec["service_name"])
        if not service and spec.get("service_id") is not None:
            service = next((row for row in services if row.get("service_id") == spec["service_id"]), None)
        if not service:
            result["errors"].append({"operation": "new_offer", "spec": spec, "error": "service_not_found_after_service_phase"})
            continue
        payload = {
            key: spec.get(key)
            for key in (
                "business_id", "promotion_id", "membership_plan_id", "is_package", "price_model",
                "regular_price", "discount_price", "discount_percent", "discount_amount",
                "is_membership_required", "is_new_customer_required", "offer_raw_text", "conditions",
            )
        }
        payload["is_active"] = False
        payload["offer_fingerprint"] = split_offer_fingerprint(
            source_key=source_key(spec),
            service_name=spec["service_name"],
            offer_raw_text=spec["offer_raw_text"],
            **{key: spec.get(key) for key in ("regular_price", "discount_price", "discount_percent", "discount_amount", "price_model")},
        )
        try:
            inserted = client.insert_rows("promo_offer_master", [payload])
            offer_id = int(inserted[0]["id"])
            item = {
                "offer_id": offer_id,
                "item_name": service["service_name"],
                "service_id": service["service_id"],
                "delivered_units": None,
            }
            if spec.get("price_model") == "per_unit" and spec.get("unit_price") is not None:
                item["unit_price"] = spec["unit_price"]
            client.insert_rows("promo_offer_items", [item])
            inserted_items = client.fetch_rows(
                "promo_offer_items",
                "offer_item_id,updated_at",
                filters={"offer_id": f"eq.{offer_id}"},
                limit=5,
            )
            for inserted_item in inserted_items:
                if inserted_item.get("updated_at"):
                    client.update_row(
                        "promo_offer_items",
                        {"offer_item_id": f"eq.{inserted_item['offer_item_id']}"},
                        {"delivered_units": None, "updated_at": now()},
                    )
            activated = client.update_row("promo_offer_master", {"id": f"eq.{offer_id}"}, {"is_active": True, "updated_at": now()})
            result["new_offers"].append({"offer_id": offer_id, "service_id": service["service_id"], "activated": bool(activated)})
        except Exception as exc:
            result["errors"].append({"operation": "new_offer", "spec": spec, "error": str(exc)})
    for operation in plan["membership_status_updates"]:
        try:
            result["membership_status_updates"].append(_apply_update(client, "clinic_memberships", "plan_id", operation))
        except Exception as exc:
            result["errors"].append({"operation": "membership_status_update", "id": operation["plan_id"], "error": str(exc)})
    return result


def build_qa(client: SupabaseRestClient) -> dict[str, Any]:
    state = load_state(client)
    offers = state["offers"]
    services = {int(row["service_id"]): row for row in state["services"]}
    promotions = {int(row["promotion_id"]): row for row in state["promotions"]}
    active = [row for row in offers if row.get("is_active")]
    no_items = [row["id"] for row in active if not item_list(row)]
    null_service = [item["offer_item_id"] for row in active for item in item_list(row) if item.get("service_id") is None]
    cross_business = []
    name_mismatch = []
    non_package_multi = []
    per_unit_one = []
    for row in active:
        items = item_list(row)
        if row.get("is_package") is False and len(items) > 1:
            non_package_multi.append(row["id"])
        if row.get("price_model") == "per_unit":
            for item in items:
                if item.get("delivered_units") == 1:
                    per_unit_one.append(item["offer_item_id"])
        for item in items:
            service = services.get(int(item.get("service_id") or 0))
            if not service:
                continue
            if service.get("business_id") != row.get("business_id"):
                cross_business.append(item["offer_item_id"])
            if service.get("service_name") != item.get("item_name"):
                name_mismatch.append(item["offer_item_id"])
    fp_groups: defaultdict[str, list[int]] = defaultdict(list)
    for row in active:
        if row.get("offer_fingerprint"):
            fp_groups[str(row["offer_fingerprint"])].append(int(row["id"]))
    inactive_parent = [row["id"] for row in active if row.get("promotion_id") is not None and not promotions.get(int(row["promotion_id"]), {}).get("is_active")]
    return {
        "offers_audited": len(offers),
        "active_offers": len(active),
        "active_offer_no_items": no_items,
        "active_item_null_service": null_service,
        "cross_business_item": cross_business,
        "item_name_mismatch": name_mismatch,
        "duplicate_fingerprint": [ids for ids in fp_groups.values() if len(ids) > 1],
        "non_package_multi_item": non_package_multi,
        "per_unit_delivered_units_one": per_unit_one,
        "active_offer_on_inactive_promotion": inactive_parent,
        "membership_required_missing": [row["id"] for row in active if row.get("membership_plan_id") is not None and row.get("is_membership_required") is not True],
        "targets": {str(offer_id): next((row for row in offers if int(row["id"]) == offer_id), None) for offer_id in (9797, 9136, 1517, 10033, 9443, 9444)},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    client = load_client()
    plan = build_plan(load_state(client))
    result: dict[str, Any] = plan
    if args.apply:
        result["mode"] = "apply"
        result["apply_result"] = apply_plan(client, plan)
        result["qa_after"] = build_qa(client)
    output = args.output or ROOT / "outputs" / "offer_plan_20260918" / f"offer_plan_20260918_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "mode": result["mode"], **plan["summary"]}, ensure_ascii=False))
    return 0 if not result.get("apply_result", {}).get("errors") else 2


if __name__ == "__main__":
    raise SystemExit(main())
