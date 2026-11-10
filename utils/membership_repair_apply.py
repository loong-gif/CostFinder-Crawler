from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv

from utils.membership_repair_common import clean_url, read_json, token_text, write_json
from utils.membership_repair_plan import VALID_ACTIONS
from utils.offer_extraction_llm import canonicalize_service_name
from utils.offer_fingerprint import compute_offer_fingerprint
from utils.promo_offer_items_db import upsert_offer_items
from utils.schema_contract import (
    CLINIC_MEMBERSHIP_SELECT,
    OFFER_MASTER_SELECT,
    OFFER_MASTER_WITH_ITEMS_SELECT,
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_PROMO_OFFER_MASTER,
)
from utils.supabase_access import fetch_all_rows, load_supabase_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def ensure_snapshot_fresh(client: Any, snapshot: Dict[str, Any]) -> None:
    current = fetch_all_rows(client, TABLE_CLINIC_MEMBERSHIPS, CLINIC_MEMBERSHIP_SELECT, order="plan_id.asc")
    old_by_id = {int(row["plan_id"]): row for row in snapshot["memberships"]}
    current_by_id = {int(row["plan_id"]): row for row in current}
    if set(old_by_id) != set(current_by_id):
        raise RuntimeError("live membership IDs changed since dry-run; refusing apply")
    for plan_id, old in old_by_id.items():
        current_row = current_by_id[plan_id]
        if any(current_row.get(field) != old.get(field) for field in ("business_id", "membership_name", "membership_price", "billing_period", "minimum_commitment_months", "benefits", "source_url")):
            raise RuntimeError(f"plan {plan_id} changed since dry-run; refusing apply")


def offer_payload(plan: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    raw = str(item.get("offer_raw_text") or "").strip()[:4000]
    raw_lower = raw.casefold()
    price_model = "per_unit" if re.search(r"(?:/|per)\s*(?:unit|syringe|vial|session|treatment)", raw_lower) else "total"
    payload: Dict[str, Any] = {
        "business_id": plan.get("business_id"),
        "membership_plan_id": plan.get("plan_id"),
        "is_active": True,
        "is_membership_required": True,
        "is_new_customer_required": False,
        "offer_raw_text": raw,
        "regular_price": item.get("regular_price"),
        "discount_price": item.get("discount_price"),
        "discount_percent": item.get("discount_percent"),
        "discount_amount": item.get("discount_amount"),
        "price_model": price_model,
        "offer_fingerprint": compute_offer_fingerprint(
            source_url=str(plan.get("final_url") or ""),
            service_name=str(item.get("service_name") or ""),
            unit_type=str(item.get("unit_type") or ""),
            regular_price=item.get("regular_price"),
            discount_price=item.get("discount_price"),
            offer_raw_text=raw,
            business_id=plan.get("business_id"),
            membership_plan_id=plan.get("plan_id"),
            discount_percent=item.get("discount_percent"),
            discount_amount=item.get("discount_amount"),
        ),
    }
    # The live price-quality trigger derives discount_amount from
    # discount_price. Supply the implied sale price when the source gives a
    # regular price plus a fixed discount, otherwise the trigger clears the
    # amount and rejects the row.
    if payload.get("discount_price") is None and payload.get("regular_price") is not None and payload.get("discount_amount") is not None:
        payload["discount_price"] = float(payload["regular_price"]) - float(payload["discount_amount"])
    return payload


def reconcile_membership_offers(client: Any, plan: Dict[str, Any]) -> Dict[str, int]:
    plan_id = int(plan["plan_id"])
    current = fetch_all_rows(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_WITH_ITEMS_SELECT, filters={"membership_plan_id": f"eq.{plan_id}", "is_active": "eq.true"}, order="id.asc")
    desired = [
        item
        for item in (plan.get("new_priced_offers") or [])
        if item.get("offer_raw_text")
        and any(item.get(field) is not None for field in ("discount_price", "discount_percent", "discount_amount"))
    ]
    used: set[int] = set()
    stats = {"updated": 0, "inserted": 0, "deactivated": 0}
    def activate(offer_id: int) -> None:
        try:
            client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{offer_id}"}, {"is_active": True})
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code not in {400, 409}:
                raise
            client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{offer_id}"}, {"is_active": False})
    for item in desired:
        matched = next((row for row in current if int(row["id"]) not in used and token_text(row.get("offer_raw_text")) == token_text(item.get("offer_raw_text"))), None)
        payload = offer_payload(plan, item)
        raw_item_rows = [{"service_name": item.get("service_name"), "quantity": item.get("quantity"), "unit_price": item.get("unit_price")}]
        services_for_business = client.fetch_rows(
            "clinic_services", "service_id,business_id,service_name",
            filters={"business_id": f"eq.{int(plan['business_id'])}"}, limit=5000,
        )
        hint = canonicalize_service_name(item.get("service_name"), item.get("offer_raw_text"))
        matches = [row for row in services_for_business if str(row.get("service_name") or "").casefold() == str(hint or "").casefold()]
        linked_item_rows = [dict(raw_item_rows[0], service_id=int(matches[0]["service_id"]))] if len(matches) == 1 else raw_item_rows
        fully_mapped = bool(linked_item_rows) and all(row.get("service_id") is not None for row in linked_item_rows)
        payload["is_active"] = False
        if matched:
            used.add(int(matched["id"]))
            existing_items = matched.get("promo_offer_items") or []
            if existing_items and all(row.get("service_id") is not None for row in existing_items):
                payload["is_active"] = True
            try:
                client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{matched['id']}"}, payload)
            except requests.HTTPError as exc:
                # A refreshed benefit may normalize to an already existing
                # fingerprint. Merge into that canonical row and retire the
                # stale text-matched duplicate.
                if exc.response is None or exc.response.status_code not in {400, 409}:
                    raise
                if all(token not in exc.response.text.lower() for token in ("duplicate key", "unique", "23505")):
                    raise
                duplicate = fetch_all_rows(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_WITH_ITEMS_SELECT, filters={"offer_fingerprint": f"eq.{payload['offer_fingerprint']}"}, order="id.asc")
                if not duplicate:
                    raise
                canonical = duplicate[0]
                client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{matched['id']}"}, {"is_active": False})
                used.add(int(canonical["id"]))
                stats["updated"] += 1
                continue
            if not (matched.get("promo_offer_items") or []) and fully_mapped:
                upsert_offer_items(client, int(matched["id"]), linked_item_rows)
                activate(int(matched["id"]))
            stats["updated"] += 1
        else:
            existing_fp = next((row for row in current if row.get("offer_fingerprint") == payload["offer_fingerprint"]), None)
            if existing_fp:
                client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{existing_fp['id']}"}, payload)
                used.add(int(existing_fp["id"]))
                offer_id = int(existing_fp["id"])
                stats["updated"] += 1
            else:
                try:
                    inserted = client.insert_rows(TABLE_PROMO_OFFER_MASTER, [payload])
                    offer_id = int(inserted[0]["id"])
                    if fully_mapped:
                        upsert_offer_items(client, offer_id, linked_item_rows)
                        activate(offer_id)
                    stats["inserted"] += 1
                except requests.HTTPError as exc:
                    if exc.response is None or exc.response.status_code != 409:
                        raise
                    concurrent = fetch_all_rows(
                        client,
                        TABLE_PROMO_OFFER_MASTER,
                        OFFER_MASTER_WITH_ITEMS_SELECT,
                        filters={"offer_fingerprint": f"eq.{payload['offer_fingerprint']}"},
                        order="id.asc",
                    )
                    if not concurrent:
                        raise
                    offer_id = int(concurrent[0]["id"])
                    used.add(offer_id)
                    stats["updated"] += 1
    for row in current:
        if int(row["id"]) not in used and row.get("is_active"):
            client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{row['id']}"}, {"is_active": False})
            stats["deactivated"] += 1
    return stats


def apply_plan(dry_run_path: Path, *, resume: bool = False) -> Dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    dry_run = read_json(dry_run_path)
    artifact_dir = Path(dry_run["artifacts"]["directory"])
    snapshot = read_json(artifact_dir / "snapshot.json")
    if dry_run.get("membership_count") != len(snapshot.get("memberships") or []) or not dry_run.get("membership_count"):
        raise RuntimeError("dry-run completeness gate failed before apply")
    if not set(dry_run.get("action_counts") or {}).issubset(VALID_ACTIONS):
        raise RuntimeError("dry-run contains an unknown action")
    client = load_supabase_client(PROJECT_ROOT)
    if not resume:
        ensure_snapshot_fresh(client, snapshot)
    else:
        current = fetch_all_rows(client, TABLE_CLINIC_MEMBERSHIPS, "plan_id", order="plan_id.asc")
        snapshot_ids = {int(row["plan_id"]) for row in snapshot["memberships"]}
        current_ids = {int(row["plan_id"]) for row in current}
        if not current_ids.issubset(snapshot_ids):
            raise RuntimeError("live membership IDs contain rows outside the dry-run snapshot; refusing resume")
    applied: List[Dict[str, Any]] = []
    for plan in sorted(dry_run["plans"], key=lambda item: int(item["plan_id"])):
        action = plan["action"]
        plan_id = int(plan["plan_id"])
        if action in {"KEEP", "REVIEW_UNVERIFIED"}:
            applied.append({"plan_id": plan_id, "action": action, "status": "skipped"})
            continue
        if action == "NORMALIZE":
            client.update_row(TABLE_CLINIC_MEMBERSHIPS, {"plan_id": f"eq.{plan_id}"}, {"benefits": plan.get("benefits_new") or []})
            applied.append({"plan_id": plan_id, "action": action, "status": "updated"})
            continue
        if action in {"UPDATE_SOURCE", "UPDATE_CONTENT"}:
            payload = {"source_url": clean_url(plan.get("final_url")), "updated_at": datetime.now(timezone.utc).isoformat()}
            if action == "UPDATE_CONTENT":
                payload.update(
                    {
                        "membership_name": plan.get("membership_name_new"),
                        "membership_price": plan.get("price_new"),
                        "billing_period": plan.get("billing_new"),
                        "minimum_commitment_months": plan.get("commitment_new"),
                        "benefits": plan.get("benefits_new") or [],
                    }
                )
            client.update_row(TABLE_CLINIC_MEMBERSHIPS, {"plan_id": f"eq.{plan_id}"}, payload)
            offer_stats = reconcile_membership_offers(client, plan) if action == "UPDATE_CONTENT" else {}
            applied.append({"plan_id": plan_id, "action": action, "status": "updated", "offer_stats": offer_stats})
            continue
        if action == "MERGE_DUPLICATE":
            survivor = int(plan["survivor_plan_id"])
            dependent = fetch_all_rows(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_SELECT, filters={"membership_plan_id": f"eq.{plan_id}"}, order="id.asc")
            for offer in dependent:
                client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{offer['id']}"}, {"membership_plan_id": survivor})
            check = fetch_all_rows(client, TABLE_PROMO_OFFER_MASTER, "id,membership_plan_id", filters={"membership_plan_id": f"eq.{plan_id}"}, order="id.asc")
            if check:
                raise RuntimeError(f"plan {plan_id} still has dependent offers after repoint")
            survivor_plan = next(item for item in dry_run["plans"] if int(item["plan_id"]) == survivor)
            if survivor_plan.get("action") == "UPDATE_CONTENT":
                reconcile_membership_offers(client, survivor_plan)
            client.delete_rows(TABLE_CLINIC_MEMBERSHIPS, {"plan_id": f"eq.{plan_id}"})
            applied.append({"plan_id": plan_id, "action": action, "status": "deleted", "survivor_plan_id": survivor, "repointed_offers": len(dependent)})
            continue
        if action == "DELETE_NOT_MEMBERSHIP":
            dependent = fetch_all_rows(client, TABLE_PROMO_OFFER_MASTER, OFFER_MASTER_SELECT, filters={"membership_plan_id": f"eq.{plan_id}"}, order="id.asc")
            for offer in dependent:
                if offer.get("promotion_id") is not None:
                    client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{offer['id']}"}, {"membership_plan_id": None})
                else:
                    client.update_row(TABLE_PROMO_OFFER_MASTER, {"id": f"eq.{offer['id']}"}, {"is_active": False, "membership_plan_id": None})
            client.delete_rows(TABLE_CLINIC_MEMBERSHIPS, {"plan_id": f"eq.{plan_id}"})
            applied.append({"plan_id": plan_id, "action": action, "status": "deleted", "handled_offers": len(dependent)})
            continue
    write_json(artifact_dir / "apply_result.json", {"applied_at": datetime.now(timezone.utc).isoformat(), "applied": applied})
    return {"artifact_dir": str(artifact_dir), "applied_count": len(applied), "apply_result": str(artifact_dir / "apply_result.json")}
