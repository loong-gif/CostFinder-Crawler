#!/usr/bin/env python3
"""Re-extract membership offers from the frozen live-evidence repair set."""
from __future__ import annotations

import argparse
import json
import requests
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.offer_extraction_llm import build_client_from_env
from utils.offer_extraction_llm import canonicalize_service_name
from utils.supabase_access import fetch_all_rows, load_supabase_client
from utils.domain_pipeline_search import llm_extract, load_schema
from utils.membership_repair_apply import offer_payload
from utils.promo_offer_items_db import upsert_offer_items

MANIFEST = ROOT / ".firecrawl" / "live_repair_manifest_20260913.json"
DRY_RUN = ROOT / ".firecrawl" / "membership_offer_reextract_20260913.json"
APPLY_RESULT = ROOT / ".firecrawl" / "membership_offer_reextract_apply_20260913.json"


def offer_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offer in payload.get("offers") or []:
        if not isinstance(offer, dict):
            continue
        items = offer.get("items") or []
        if not isinstance(items, list):
            continue
        items = [item for item in items if isinstance(item, dict)]
        if not items:
            continue
        first = items[0]
        price_model = offer.get("price_model")
        if price_model == "from" and first.get("unit_type") in {"unit", "units"} and first.get("unit_price") is not None:
            price_model = "per_unit"
        discount_price = offer.get("discount_price")
        if discount_price is None and price_model == "per_unit" and first.get("unit_price") is not None:
            discount_price = first.get("unit_price")
        rows.append(
            {
                "service_name": first.get("service_name"),
                "offer_raw_text": str(offer.get("offer_raw_text") or "").strip(),
                "price_model": price_model,
                "regular_price": offer.get("regular_price"),
                "discount_price": discount_price,
                "discount_percent": offer.get("discount_percent"),
                "discount_amount": offer.get("discount_amount"),
                "unit_type": first.get("unit_type"),
                "unit_price": first.get("unit_price"),
                "quantity": first.get("quantity"),
                "conditions": offer.get("conditions") or [],
                "item_rows": [
                    {
                        "service_name": item.get("service_name"),
                        "unit_price": item.get("unit_price"),
                        "quantity": item.get("quantity"),
                    }
                    for item in items
                ],
            }
        )
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (
            normalized_raw(row.get("offer_raw_text")),
            row.get("regular_price"),
            row.get("discount_price"),
            row.get("discount_percent"),
            row.get("discount_amount"),
            json.dumps(row.get("conditions") or [], ensure_ascii=False, sort_keys=True),
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            continue
        existing_items = existing.setdefault("item_rows", [])
        for item in row.get("item_rows") or []:
            if item not in existing_items:
                existing_items.append(item)
    return list(merged.values())


def active_offer_snapshot(client: Any, plan_id: int) -> list[dict[str, Any]]:
    return fetch_all_rows(
        client,
        "promo_offer_master",
        "id,updated_at,offer_raw_text,is_active,promotion_id,membership_plan_id,promo_offer_items(offer_item_id,service_id,item_name,quantity:delivered_units,unit_price)",
        filters={"membership_plan_id": f"eq.{plan_id}", "is_active": "eq.true"},
        order="id.asc",
    )


def normalized_raw(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def condition_requirements(item: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    raw = item.get("conditions")
    if not isinstance(raw, list):
        return None, "conditions_not_array"
    requirements: list[str] = []
    for condition in raw:
        if isinstance(condition, str):
            requirement = condition.strip()
        elif isinstance(condition, dict):
            requirement = str(condition.get("requirement") or "").strip()
            if not str(condition.get("evidence") or "").strip():
                return None, "condition_missing_evidence"
        else:
            return None, "condition_not_text"
        if not requirement:
            return None, "condition_missing_requirement"
        requirements.append(requirement)
    return requirements, None


def resolve_services(client: Any, business_id: int, desired: list[dict[str, Any]]) -> list[dict[str, Any]]:
    services = client.fetch_rows(
        "clinic_services",
        "service_id,business_id,service_name",
        filters={"business_id": f"eq.{int(business_id)}"},
        limit=5000,
    )
    resolved: list[dict[str, Any]] = []
    for item in desired:
        item_resolutions = []
        for raw_item in item.get("item_rows") or [item]:
            hint = canonicalize_service_name(raw_item.get("service_name"), item.get("offer_raw_text"))
            matches = [
                row for row in services
                if str(row.get("service_name") or "").casefold() == str(hint or "").casefold()
            ]
            item_resolutions.append(
                {
                    "service_name": raw_item.get("service_name"),
                    "canonical_service_name": hint,
                    "candidate_service_ids": [row.get("service_id") for row in matches],
                    "candidate_service_names": [row.get("service_name") for row in matches],
                }
            )
        resolved.append(
            {
                "offer_raw_text": item.get("offer_raw_text"),
                "items": item_resolutions,
            }
        )
    return resolved


def validate_desired_shape(plan: dict[str, Any]) -> str | None:
    desired = plan.get("desired_offers") or []
    raw_keys = [normalized_raw(item.get("offer_raw_text")) for item in desired]
    if any(not key for key in raw_keys):
        return "offer_raw_text_missing"
    if len(raw_keys) != len(set(raw_keys)):
        return "multi_item_or_duplicate_offer_text"
    resolutions = plan.get("service_resolution") or []
    if len(resolutions) != len(desired):
        return "service_resolution_missing"
    if any(
        len(item.get("candidate_service_ids") or []) != 1
        for row in resolutions
        for item in row.get("items") or []
    ):
        return "service_mapping_not_unique"
    for item in desired:
        _, error = condition_requirements(item)
        if error:
            return error
    return None


def apply_conditions(client: Any, plan_id: int, desired: list[dict[str, Any]], active: list[dict[str, Any]]) -> int:
    updated = 0
    for item in desired:
        requirements, error = condition_requirements(item)
        if error or requirements is None:
            raise RuntimeError(f"plan {plan_id} has invalid conditions: {error}")
        matches = [
            row for row in active
            if normalized_raw(row.get("offer_raw_text")) == normalized_raw(item.get("offer_raw_text"))
        ]
        if len(matches) != 1:
            raise RuntimeError(f"plan {plan_id} offer text did not resolve uniquely after reconciliation")
        client.update_row(
            "promo_offer_master",
            {"id": f"eq.{int(matches[0]['id'])}"},
            {"conditions": requirements},
        )
        updated += 1
    return updated


def apply_price_models(client: Any, desired: list[dict[str, Any]], active: list[dict[str, Any]]) -> int:
    updated = 0
    for item in desired:
        if item.get("price_model") != "per_unit":
            continue
        matches = [
            row for row in active
            if normalized_raw(row.get("offer_raw_text")) == normalized_raw(item.get("offer_raw_text"))
        ]
        if len(matches) != 1:
            raise RuntimeError("per-unit offer text did not resolve uniquely after reconciliation")
        client.update_row(
            "promo_offer_master",
            {"id": f"eq.{int(matches[0]['id'])}"},
            {"price_model": "per_unit"},
        )
        updated += 1
    return updated


def service_id_set(row: dict[str, Any]) -> set[int]:
    return {
        int(item["service_id"])
        for item in row.get("promo_offer_items") or []
        if item.get("service_id") is not None
    }


def safe_reconcile_membership_offers(client: Any, plan: dict[str, Any], desired: list[dict[str, Any]]) -> dict[str, int]:
    plan_id = int(plan["plan_id"])
    select = "id,updated_at,offer_fingerprint,offer_raw_text,is_active,price_model,discount_price,discount_percent,discount_amount,conditions,promo_offer_items(offer_item_id,service_id,item_name,quantity:delivered_units,unit_price)"
    current = fetch_all_rows(client, "promo_offer_master", select, filters={"membership_plan_id": f"eq.{plan_id}", "is_active": "eq.true"}, order="id.asc")
    all_rows = fetch_all_rows(client, "promo_offer_master", select, filters={"membership_plan_id": f"eq.{plan_id}"}, order="id.asc")
    resolutions = {normalized_raw(item.get("offer_raw_text")): item for item in plan.get("service_resolution") or []}
    used: set[int] = set()
    stats = {"updated": 0, "inserted": 0, "deactivated": 0}
    for item in desired:
        resolution = resolutions.get(normalized_raw(item.get("offer_raw_text"))) or {}
        item_rows = item.get("item_rows") or [item]
        linked_items = []
        for raw_item, resolved_item in zip(item_rows, resolution.get("items") or []):
            candidate_ids = resolved_item.get("candidate_service_ids") or []
            if len(candidate_ids) != 1:
                raise RuntimeError(f"plan {plan_id} has unresolved service mapping")
            linked_items.append({
                "service_name": raw_item.get("service_name"),
                "service_id": int(candidate_ids[0]),
                "quantity": raw_item.get("quantity"),
                "unit_price": raw_item.get("unit_price"),
            })
        payload = offer_payload(plan, item)
        payload["is_active"] = True
        payload["price_model"] = item.get("price_model") or payload.get("price_model")
        requirements, error = condition_requirements(item)
        if error or requirements is None:
            raise RuntimeError(f"plan {plan_id} has invalid conditions: {error}")
        payload["conditions"] = requirements
        candidate_rows = [
            row for row in current
            if normalized_raw(row.get("offer_raw_text")) == normalized_raw(item.get("offer_raw_text"))
        ]
        if not candidate_rows:
            wanted_ids = {int(raw["service_id"]) for raw in linked_items}
            candidate_rows = [
                row for row in all_rows
                if service_id_set(row) == wanted_ids
                and row.get("discount_price") == payload.get("discount_price")
            ]
        if len(candidate_rows) > 1:
            candidate_rows = sorted(candidate_rows, key=lambda row: int(row["id"]))[:1]
        matched = candidate_rows[0] if candidate_rows else next(
            (row for row in all_rows if row.get("offer_fingerprint") == payload.get("offer_fingerprint")),
            None,
        )
        if matched:
            offer_id = int(matched["id"])
            payload["is_active"] = False
            client.update_row("promo_offer_master", {"id": f"eq.{offer_id}"}, payload)
            upsert_offer_items(client, offer_id, linked_items)
            client.update_row("promo_offer_master", {"id": f"eq.{offer_id}"}, {"is_active": True})
            used.add(offer_id)
            stats["updated"] += 1
        else:
            try:
                payload["is_active"] = False
                inserted = client.insert_rows("promo_offer_master", [payload])
                offer_id = int(inserted[0]["id"])
                upsert_offer_items(client, offer_id, linked_items)
                client.update_row("promo_offer_master", {"id": f"eq.{offer_id}"}, {"is_active": True})
                used.add(offer_id)
                stats["inserted"] += 1
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code not in {400, 409}:
                    raise
                duplicate = fetch_all_rows(client, "promo_offer_master", select, filters={"offer_fingerprint": f"eq.{payload['offer_fingerprint']}"}, order="id.asc")
                if len(duplicate) != 1:
                    raise
                offer_id = int(duplicate[0]["id"])
                payload["is_active"] = False
                client.update_row("promo_offer_master", {"id": f"eq.{offer_id}"}, payload)
                upsert_offer_items(client, offer_id, linked_items)
                client.update_row("promo_offer_master", {"id": f"eq.{offer_id}"}, {"is_active": True})
                used.add(offer_id)
                stats["updated"] += 1
    for row in current:
        if int(row["id"]) not in used and row.get("is_active"):
            client.update_row("promo_offer_master", {"id": f"eq.{int(row['id'])}"}, {"is_active": False})
            stats["deactivated"] += 1
    return stats


def build_dry_run() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ready = [item for item in manifest["entries"] if item.get("record_type") == "membership" and item.get("decision") == "ready"]
    client = load_supabase_client(ROOT)
    memberships = {row["plan_id"]: row for row in fetch_all_rows(client, "clinic_memberships", "*")}
    llm = build_client_from_env()
    if llm is None:
        raise RuntimeError("LLM_API_URL/LLM_API_KEY/LLM_MODEL are required")
    schema = load_schema("offer_extraction_schema.json")
    plans: list[dict[str, Any]] = []
    for entry in ready:
        plan_id = int(entry["record_id"])
        row = memberships[plan_id]
        source = str(entry.get("final_url") or entry.get("source_url") or row.get("source_url") or "")
        evidence = "Membership tier: " + str(row.get("membership_name") or "") + "\n" + "\n".join(entry.get("proposed_content") or [])
        response = llm_extract(
            llm,
            schema,
            task=(
                "Extract only concrete purchasable service offers that are explicitly member-priced in this membership evidence. "
                "Do not extract the membership fee itself. Keep a per-unit price as per_unit with unit_price; keep discounts and conditions verbatim. "
                "If no service-level price is explicit, return an empty offers array. Do not invent regular prices or quantities."
            ),
            source_url=source,
            markdown=evidence,
        )
        desired = offer_rows(response)
        priced_desired = [
            item for item in desired
            if item.get("offer_raw_text")
            and any(item.get(field) is not None for field in ("discount_price", "discount_percent", "discount_amount"))
        ]
        plans.append(
            {
                "plan_id": plan_id,
                "business_id": row.get("business_id"),
                "membership_name": row.get("membership_name"),
                "final_url": source,
                "membership_updated_at": row.get("updated_at"),
                "old_active_offers": active_offer_snapshot(client, plan_id),
                "llm_response": response,
                "desired_offers": priced_desired,
                "service_resolution": resolve_services(client, int(row["business_id"]), priced_desired),
            }
        )
    return {
        "artifact_type": "membership_offer_reextract",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(MANIFEST),
        "schema": "schema/offer_extraction_schema.json",
        "read_only": True,
        "plans": plans,
    }


def verify_plan_snapshots(client: Any, plan: dict[str, Any]) -> None:
    row = fetch_all_rows(client, "clinic_memberships", "plan_id,updated_at", filters={"plan_id": f"eq.{int(plan['plan_id'])}"})
    if len(row) != 1 or row[0].get("updated_at") != plan.get("membership_updated_at"):
        raise RuntimeError(f"membership plan {plan['plan_id']} changed since offer dry-run")
    current = active_offer_snapshot(client, int(plan["plan_id"]))
    expected = {int(item["id"]): item.get("updated_at") for item in plan.get("old_active_offers") or []}
    actual = {int(item["id"]): item.get("updated_at") for item in current}
    if expected != actual:
        raise RuntimeError(f"active offers for plan {plan['plan_id']} changed since offer dry-run")


def apply_dry_run(payload: dict[str, Any]) -> dict[str, Any]:
    client = load_supabase_client(ROOT)
    applied: list[dict[str, Any]] = []
    for plan in payload.get("plans") or []:
        if plan.get("prevalidated"):
            applied.append({"plan_id": plan["plan_id"], "status": "prevalidated", "reason": plan.get("prevalidated_reason")})
            continue
        verify_plan_snapshots(client, plan)
        desired = list(plan.get("desired_offers") or [])
        shape_error = validate_desired_shape(plan) if desired else None
        current_active = active_offer_snapshot(client, int(plan["plan_id"]))
        if shape_error:
            applied.append({
                "plan_id": plan["plan_id"],
                "status": "review",
                "reason": shape_error,
                "desired_offer_count": len(desired),
                "active_offer_count": len(current_active),
            })
            continue
        if not desired and current_active:
            applied.append({
                "plan_id": plan["plan_id"],
                "status": "review",
                "reason": "no_structured_offers_but_existing_active_offers",
                "desired_offer_count": 0,
                "active_offer_count": len(current_active),
            })
            continue
        stats = safe_reconcile_membership_offers(client, plan, desired)
        active = active_offer_snapshot(client, int(plan["plan_id"]))
        valid_items = all(
            item.get("promo_offer_items")
            and all(item_row.get("service_id") is not None for item_row in item.get("promo_offer_items") or [])
            for item in active
        )
        conditions_updated = len(desired) if desired and valid_items else 0
        price_models_updated = sum(item.get("price_model") == "per_unit" for item in desired) if desired and valid_items else 0
        if desired and valid_items:
            active = active_offer_snapshot(client, int(plan["plan_id"]))
        complete = (not desired and not active) or (
            bool(desired)
            and len(active) == len(desired)
            and valid_items
            and conditions_updated == len(desired)
            and price_models_updated == sum(item.get("price_model") == "per_unit" for item in desired)
            and all(
                sum(normalized_raw(row.get("offer_raw_text")) == normalized_raw(item.get("offer_raw_text")) for row in active) == 1
                for item in desired
            )
        )
        if complete:
            client.update_row("clinic_memberships", {"plan_id": f"eq.{int(plan['plan_id'])}"}, {"is_offer_extracted": True, "updated_at": datetime.now(timezone.utc).isoformat()})
        applied.append({"plan_id": plan["plan_id"], "status": "updated" if complete else "review", "desired_offer_count": len(desired), "complete": complete, "offer_stats": stats, "conditions_updated": conditions_updated, "price_models_updated": price_models_updated, "active_offer_count": len(active)})
    return {"applied_at": datetime.now(timezone.utc).isoformat(), "dry_run": False, "plans": applied}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="apply the existing frozen dry-run artifact")
    args = parser.parse_args()
    if args.apply:
        payload = json.loads(DRY_RUN.read_text(encoding="utf-8"))
        result = apply_dry_run(payload)
        APPLY_RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"result": str(APPLY_RESULT), "dry_run": False, "plans": len(result["plans"])}, ensure_ascii=False, indent=2))
        return 0
    payload = build_dry_run()
    DRY_RUN.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"result": str(DRY_RUN), "dry_run": True, "plans": len(payload["plans"]), "offers": sum(len(p["desired_offers"]) for p in payload["plans"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
