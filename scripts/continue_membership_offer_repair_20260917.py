#!/usr/bin/env python3
"""Continue the source-backed clinic_memberships repair.

This is intentionally narrow: it handles the nine remaining findings from the
execution plan and leaves only incompletely mapped broad-service plans in review.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.offer_fingerprint import compute_offer_fingerprint
from utils.clinic_service_taxonomy import service_name_enum_hint
from utils.schema_contract import OFFER_MASTER_SELECT
from utils.supabase_access import fetch_all_rows, load_supabase_client

ARTIFACT_DIR = ROOT / ".firecrawl" / "clinic_memberships_plan_20260917"
DRY_RUN_ARTIFACT = ARTIFACT_DIR / "continued_repair_dry_run_20260917.json"
APPLY_ARTIFACT = ARTIFACT_DIR / "continued_repair_apply_20260917.json"
NOB_HILL_URL = "https://nobhillaesthetics.com/pages/membership"
CALIFORNIA_URL = "https://californiacosmetics.com/products/botox-more-member"
SKIN_MAISON_URL = "https://skinmaison.com/beauty-bank"
SKIN_GOALS_URL = "https://californiacosmetics.com/products/skin-goals-member"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def unique(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and norm(text) not in seen:
            out.append(text)
            seen.add(norm(text))
    return out


def membership_rows(client: Any) -> List[Dict[str, Any]]:
    return fetch_all_rows(
        client,
        "clinic_memberships",
        "plan_id,business_id,membership_name,membership_price,billing_period,minimum_commitment_months,benefits,source_url,is_offer_extracted,is_ocr_required,need_browser,updated_at",
        order="plan_id.asc",
    )


def service_rows(client: Any) -> List[Dict[str, Any]]:
    return fetch_all_rows(
        client,
        "clinic_services",
        "service_id,business_id,service_name,regular_price,unit_type,source_url,updated_at",
        order="service_id.asc",
    )


def offer_rows(client: Any) -> List[Dict[str, Any]]:
    return fetch_all_rows(
        client,
        "promo_offer_master",
        OFFER_MASTER_SELECT,
        filters={"membership_plan_id": "not.is.null"},
        order="id.asc",
    )


def item_rows(client: Any) -> List[Dict[str, Any]]:
    return fetch_all_rows(
        client,
        "promo_offer_items",
        "offer_item_id,offer_id,service_id,item_name,delivered_units,unit_price,updated_at",
        order="offer_item_id.asc",
    )


def snapshot(row: Dict[str, Any], keys: Iterable[str]) -> Dict[str, Any]:
    return {key: row.get(key) for key in keys}


def plan_by_id(rows: List[Dict[str, Any]], plan_id: int) -> Dict[str, Any]:
    for row in rows:
        if int(row["plan_id"]) == plan_id:
            return row
    raise RuntimeError(f"missing membership plan {plan_id}")


def service_insert_specs() -> List[Dict[str, Any]]:
    return [
        {"business_id": 3040, "service_name": "Hydrafacial", "unit_type": "session", "source_url": NOB_HILL_URL},
        {"business_id": 3040, "service_name": "Halo Laser", "unit_type": "session", "source_url": NOB_HILL_URL},
        {"business_id": 3040, "service_name": "BBL Photofacial", "unit_type": "session", "source_url": NOB_HILL_URL},
        {"business_id": 3040, "service_name": "Morpheus8", "unit_type": "session", "source_url": NOB_HILL_URL},
        {"business_id": 3020, "service_name": "Body Contouring", "unit_type": "session", "source_url": CALIFORNIA_URL},
        {"business_id": 3020, "service_name": "Pearl Fractional", "unit_type": "session", "source_url": SKIN_GOALS_URL},
    ]


def offer_specs() -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []

    def add(plan_id: int, service_name: str, raw: str, source_url: str) -> None:
        specs.append(
            {
                "plan_id": plan_id,
                "service_name": service_name,
                "offer_raw_text": raw,
                "discount_percent": 25.0 if plan_id in (425, 426) else 10.0,
                "source_url": source_url,
            }
        )

    for plan_id in (425, 426):
        tier = "Gold" if plan_id == 425 else "Platinum"
        for service_name in ("Hydrafacial", "Halo Laser", "BBL Photofacial", "Morpheus8"):
            add(plan_id, service_name, f"{tier} membership: 25% off {service_name}", NOB_HILL_URL)
    for service_name in ("Facial", "SkinPen Microneedling", "IPL Photofacial", "Body Contouring"):
        add(697, service_name, f"TOX & More Membership: 10% off {service_name}", CALIFORNIA_URL)
    add(700, "Facial", "Deluxe Hydrafacial Membership: Skin Treatments 10% off", SKIN_MAISON_URL)
    specs.append(
        {
            "plan_id": 699,
            "service_name": "Pearl Fractional",
            "offer_raw_text": "Skin Goals Membership: selectable Pearl Fractional med-spa experience",
            "discount_percent": 100.0,
            "discount_price": 0.0,
            "quantity": 1,
            "source_url": SKIN_GOALS_URL,
        }
    )
    return specs


def desired_benefits(rows: List[Dict[str, Any]]) -> Dict[int, List[str]]:
    current = {int(row["plan_id"]): row for row in rows}
    rollover = "If a monthly treatment is missed, one treatment may roll over per 6-month period"
    return {
        323: unique([*(current[323].get("benefits") or []), rollover]),
        461: [],
        462: [],
    }


def duplicate_offer_ids(client: Any) -> List[Dict[str, Any]]:
    ids = [7830, 8751, 7821, 8833, 8835, 7878]
    rows = fetch_all_rows(
        client,
        "promo_offer_master",
        "id,membership_plan_id,offer_raw_text,is_active,updated_at",
        filters={"id": "in.(" + ",".join(map(str, ids)) + ")"},
        order="id.asc",
    )
    return [snapshot(row, ("id", "membership_plan_id", "offer_raw_text", "is_active", "updated_at")) for row in rows if row.get("is_active")]


def build_plan(client: Any) -> Dict[str, Any]:
    memberships = membership_rows(client)
    services = service_rows(client)
    offers = offer_rows(client)
    items = item_rows(client)
    by_service = {(int(row["business_id"]), norm(row["service_name"])): row for row in services}
    needed_services = [
        spec for spec in service_insert_specs()
        if (int(spec["business_id"]), norm(spec["service_name"])) not in by_service
    ]
    service_ids = {int(row["service_id"]): row for row in services}
    item_by_offer = defaultdict(list)
    for item in items:
        item_by_offer[int(item["offer_id"])].append(item)
    offer_targets = []
    for spec in offer_specs():
        plan = plan_by_id(memberships, int(spec["plan_id"]))
        existing = []
        for offer in offers:
            if int(offer.get("membership_plan_id") or 0) != int(spec["plan_id"]):
                continue
            if offer.get("is_active") is not True:
                continue
            for item in item_by_offer[int(offer["id"])]:
                service = service_ids.get(int(item.get("service_id") or 0))
                if service and norm(service["service_name"]) == norm(spec["service_name"]):
                    if float(offer.get("discount_percent") or 0) == float(spec["discount_percent"]):
                        existing.append(int(offer["id"]))
        offer_targets.append({"spec": spec, "plan_name": plan["membership_name"], "existing_semantic_offer_ids": existing})
    plan_changes = []
    benefits = desired_benefits(memberships)
    for plan_id, new_benefits in benefits.items():
        row = plan_by_id(memberships, plan_id)
        payload = {"benefits": new_benefits}
        if plan_id in (461, 462):
            payload.update({"is_offer_extracted": False, "is_ocr_required": True, "need_browser": True})
        plan_changes.append({"plan_id": plan_id, "before": snapshot(row, ("plan_id", "benefits", "is_offer_extracted", "is_ocr_required", "need_browser", "updated_at")), "payload": payload})
    extraction_changes = []
    for plan_id, value in ((425, True), (426, True), (697, True), (700, True), (483, False), (484, False), (699, True)):
        row = plan_by_id(memberships, plan_id)
        if row.get("is_offer_extracted") != value:
            extraction_changes.append({"plan_id": plan_id, "before": snapshot(row, ("plan_id", "is_offer_extracted", "updated_at")), "is_offer_extracted": value})
    return {
        "created_at": now(),
        "scope": "remaining nine findings from clinic_memberships full repair plan",
        "source_urls": [NOB_HILL_URL, CALIFORNIA_URL, SKIN_MAISON_URL, SKIN_GOALS_URL],
        "screenshot_files": [
            str(ARTIFACT_DIR / "screenshots" / "nob_hill_membership.png"),
        ],
        "firecrawl_endpoint": "http://192.168.1.68:3002/v1/scrape",
        "service_inserts": needed_services,
        "offer_targets": offer_targets,
        "duplicate_deactivations": duplicate_offer_ids(client),
        "membership_changes": plan_changes,
        "extraction_changes": extraction_changes,
        "unresolved_review": [
            {"plan_ids": [483, 484], "reason": "Source says 10% off all services and names unsupported/incompletely mapped categories; leave false instead of creating partial downstream Offers."},
        ],
    }


def verify_before(actual: Dict[str, Any], before: Dict[str, Any], label: str) -> None:
    for key, expected in before.items():
        if actual.get(key) != expected:
            raise RuntimeError(f"concurrency conflict {label}:{key}: expected {expected!r}, got {actual.get(key)!r}")


def ensure_services(client: Any, specs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    created = []
    current = service_rows(client)
    by_key = {(int(row["business_id"]), norm(row["service_name"])): row for row in current}
    for spec in specs:
        canonical_name = service_name_enum_hint(spec.get("service_name"))
        if canonical_name is None:
            raise RuntimeError(f"unsupported clinic service enum: {spec.get('service_name')!r}")
        key = (int(spec["business_id"]), norm(canonical_name))
        if key in by_key:
            continue
        payload = {**spec, "service_name": canonical_name, "created_at": now(), "updated_at": now()}
        inserted = client.insert_rows("clinic_services", [payload])
        if len(inserted) != 1:
            raise RuntimeError(f"service insert failed: {spec}")
        by_key[key] = inserted[0]
        created.append(snapshot(inserted[0], ("service_id", "business_id", "service_name", "source_url")))
    return created


def find_offer_for_spec(offers: List[Dict[str, Any]], items: List[Dict[str, Any]], plan_id: int, service_id: int, discount_percent: float) -> Dict[str, Any] | None:
    item_by_offer = defaultdict(list)
    for item in items:
        item_by_offer[int(item["offer_id"])].append(item)
    candidates = []
    for offer in offers:
        if int(offer.get("membership_plan_id") or 0) != plan_id or float(offer.get("discount_percent") or 0) != discount_percent:
            continue
        if any(int(item.get("service_id") or 0) == service_id for item in item_by_offer[int(offer["id"])]):
            candidates.append(offer)
    candidates.sort(key=lambda row: (not bool(row.get("is_active")), int(row["id"])))
    return candidates[0] if candidates else None


def apply_offer(client: Any, spec: Dict[str, Any], plan: Dict[str, Any], service: Dict[str, Any], offers: List[Dict[str, Any]], items: List[Dict[str, Any]]) -> Dict[str, Any]:
    plan_id = int(plan["plan_id"])
    discount_percent = float(spec["discount_percent"])
    raw = str(spec["offer_raw_text"])
    conditions = unique([
        f"Applicable membership tier: {plan['membership_name']}",
        "Member pricing",
    ])
    fp = compute_offer_fingerprint(
        source_url=str(plan.get("source_url") or spec["source_url"]),
        service_name=str(service["service_name"]),
        unit_type=str(service.get("unit_type") or ""),
        regular_price=None,
        discount_price=spec.get("discount_price"),
        offer_raw_text=raw,
        business_id=int(plan["business_id"]),
        membership_plan_id=plan_id,
        discount_percent=discount_percent,
        discount_amount=None,
    )
    payload = {
        "business_id": int(plan["business_id"]),
        "membership_plan_id": plan_id,
        "is_package": False,
        "is_active": False,
        "is_new_customer_required": False,
        "is_membership_required": True,
        "offer_fingerprint": fp,
        "offer_raw_text": raw,
        "price_model": "total",
        "discount_percent": discount_percent,
        "conditions": conditions,
        "updated_at": now(),
    }
    if spec.get("discount_price") is not None:
        payload["discount_price"] = spec["discount_price"]
    offer = find_offer_for_spec(offers, items, plan_id, int(service["service_id"]), discount_percent)
    if offer:
        updated = client.update_row("promo_offer_master", {"id": f"eq.{offer['id']}"}, payload)
        offer_id = int(updated[0]["id"]) if updated else int(offer["id"])
    else:
        inserted = client.insert_rows("promo_offer_master", [payload])
        if len(inserted) != 1:
            raise RuntimeError(f"offer insert failed: {spec}")
        offer_id = int(inserted[0]["id"])
    current_items = client.fetch_rows("promo_offer_items", "offer_item_id,service_id,item_name,delivered_units,unit_price", filters={"offer_id": f"eq.{offer_id}"}, limit=20)
    expected_quantity = spec.get("quantity")
    correct = len(current_items) == 1 and int(current_items[0].get("service_id") or 0) == int(service["service_id"]) and current_items[0].get("item_name") == service["service_name"] and current_items[0].get("delivered_units") == expected_quantity and current_items[0].get("unit_price") is None
    if not correct:
        if current_items:
            client.delete_rows("promo_offer_items", {"offer_id": f"eq.{offer_id}"})
        item = {"offer_id": offer_id, "service_id": int(service["service_id"]), "item_name": service["service_name"], "created_at": now(), "updated_at": now()}
        if expected_quantity is not None:
            item["delivered_units"] = expected_quantity
        client.insert_rows("promo_offer_items", [item])
    activated = client.update_row("promo_offer_master", {"id": f"eq.{offer_id}"}, {"is_active": True, "updated_at": now()})
    return {"offer_id": offer_id, "plan_id": plan_id, "service_id": int(service["service_id"]), "service_name": service["service_name"], "updated": bool(offer), "activated": bool(activated)}


def apply_plan(client: Any, plan: Dict[str, Any]) -> Dict[str, Any]:
    created_services = ensure_services(client, plan["service_inserts"])
    memberships = membership_rows(client)
    services = service_rows(client)
    service_by_key = {(int(row["business_id"]), norm(row["service_name"])): row for row in services}
    plans = {int(row["plan_id"]): row for row in memberships}
    offers = offer_rows(client)
    items = item_rows(client)
    applied_offers = []
    for target in plan["offer_targets"]:
        spec = target["spec"]
        membership = plans[int(spec["plan_id"])]
        service = service_by_key.get((int(membership["business_id"]), norm(spec["service_name"])))
        if service is None:
            raise RuntimeError(f"missing service after insert: {spec}")
        applied_offers.append(apply_offer(client, spec, membership, service, offers, items))
        offers = offer_rows(client)
        items = item_rows(client)
    deactivated = []
    for operation in plan["duplicate_deactivations"]:
        current = client.fetch_rows("promo_offer_master", "id,membership_plan_id,offer_raw_text,is_active,updated_at", filters={"id": f"eq.{operation['id']}"}, limit=1)
        if not current or not current[0].get("is_active"):
            continue
        verify_before(current[0], operation, f"duplicate offer {operation['id']}")
        result = client.update_row("promo_offer_master", {"id": f"eq.{operation['id']}", "updated_at": f"eq.{operation['updated_at']}"}, {"is_active": False, "updated_at": now()})
        if len(result) != 1:
            raise RuntimeError(f"deactivation failed for {operation['id']}")
        deactivated.append(int(operation["id"]))
    changed_plans = []
    for operation in plan["membership_changes"]:
        current = client.fetch_rows("clinic_memberships", "plan_id,benefits,is_offer_extracted,is_ocr_required,need_browser,updated_at", filters={"plan_id": f"eq.{operation['plan_id']}"}, limit=1)
        if not current:
            raise RuntimeError(f"missing plan {operation['plan_id']}")
        if current[0].get("benefits") == operation["payload"].get("benefits") and all(current[0].get(k) == v for k, v in operation["payload"].items() if k != "benefits"):
            continue
        verify_before(current[0], operation["before"], f"plan {operation['plan_id']}")
        payload = {**operation["payload"], "updated_at": now()}
        result = client.update_row("clinic_memberships", {"plan_id": f"eq.{operation['plan_id']}", "updated_at": f"eq.{operation['before']['updated_at']}"}, payload)
        if len(result) != 1:
            raise RuntimeError(f"membership update failed for {operation['plan_id']}")
        changed_plans.append(int(operation["plan_id"]))
    for operation in plan["extraction_changes"]:
        current = client.fetch_rows("clinic_memberships", "plan_id,is_offer_extracted,updated_at", filters={"plan_id": f"eq.{operation['plan_id']}"}, limit=1)
        if not current:
            raise RuntimeError(f"missing extraction plan {operation['plan_id']}")
        if current[0].get("is_offer_extracted") == operation["is_offer_extracted"]:
            continue
        verify_before(current[0], operation["before"], f"extraction plan {operation['plan_id']}")
        result = client.update_row("clinic_memberships", {"plan_id": f"eq.{operation['plan_id']}", "updated_at": f"eq.{operation['before']['updated_at']}"}, {"is_offer_extracted": operation["is_offer_extracted"], "updated_at": now()})
        if len(result) != 1:
            raise RuntimeError(f"extraction update failed for {operation['plan_id']}")
    return {
        "applied_at": now(),
        "created_services": created_services,
        "applied_offers": applied_offers,
        "deactivated_duplicate_offers": deactivated,
        "changed_memberships": changed_plans,
        "unresolved_review": plan["unresolved_review"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    client = load_supabase_client(ROOT)
    if not args.apply:
        plan = build_plan(client)
        DRY_RUN_ARTIFACT.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"mode": "dry_run", "artifact": str(DRY_RUN_ARTIFACT), "service_inserts": len(plan["service_inserts"]), "offers": len(plan["offer_targets"]), "duplicate_deactivations": len(plan["duplicate_deactivations"]), "membership_changes": len(plan["membership_changes"]), "extraction_changes": len(plan["extraction_changes"])}, ensure_ascii=False))
        return 0
    plan = json.loads(DRY_RUN_ARTIFACT.read_text(encoding="utf-8"))
    result = apply_plan(client, plan)
    APPLY_ARTIFACT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"mode": "applied", "artifact": str(APPLY_ARTIFACT), **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
