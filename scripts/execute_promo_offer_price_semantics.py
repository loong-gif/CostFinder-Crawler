#!/usr/bin/env python3
"""Dry-run and apply the text-backed Promo Offer price repair plan."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from utils.promo_offer_price_quality import audit_offer_prices
from utils.promo_offer_price_semantics import SemanticRepairPlan, build_semantic_repair_plan
from utils.supabase_access import fetch_all_rows
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key


SELECT = (
    "id,regular_price,discount_price,discount_amount,discount_percent,price_model,is_package,"
    "offer_raw_text,updated_at,"
    "promo_offer_items(offer_item_id,service_id,quantity:delivered_units,unit_price,updated_at)"
)
ITEM_SELECT = "offer_item_id,offer_id,service_id,quantity:delivered_units,unit_price,updated_at"
SERVICE_SELECT = "service_id,service_name,unit_type"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="应用修复；默认只生成 dry-run 报告")
    parser.add_argument("--output-dir", default=str(ROOT / "outputs" / "promo_offer_price_repairs"))
    return parser.parse_args()


def reader() -> SupabaseRestClient:
    load_dotenv(ROOT / ".env")
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_PUBLISHABLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
    if not url or not key:
        raise RuntimeError("缺少 SUPABASE_URL 或只读 Supabase 凭据")
    return SupabaseRestClient(url, key)


def writer() -> SupabaseRestClient:
    load_dotenv(ROOT / ".env")
    url = os.getenv("SUPABASE_URL")
    if not url:
        raise RuntimeError("缺少 SUPABASE_URL")
    return SupabaseRestClient(url, get_supabase_writer_key())


def load_rows(client: SupabaseRestClient) -> list[dict[str, Any]]:
    return fetch_all_rows(client, "promo_offer_master", SELECT, page_size=1000, order="id.asc")


def _apply_master_updates(client: SupabaseRestClient, updates: list[dict[str, Any]]) -> dict[str, Any]:
    applied: list[int] = []
    conflicts: list[dict[str, Any]] = []
    for update in updates:
        payload = {key: value for key, value in update.items() if key not in {"id", "updated_at"}}
        returned = client.update_row(
            "promo_offer_master",
            {"id": f"eq.{update['id']}", "updated_at": f"eq.{update['updated_at']}"},
            payload,
        )
        if returned:
            applied.append(int(update["id"]))
        else:
            conflicts.append({"table": "promo_offer_master", "id": update["id"]})
    return {"applied": applied, "conflicts": conflicts}


def _apply_item_updates(client: SupabaseRestClient, updates: list[dict[str, Any]]) -> dict[str, Any]:
    applied: list[int] = []
    conflicts: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for update in updates:
        payload = {
            "delivered_units" if key == "quantity" else key: value
            for key, value in update.items()
            if key not in {"offer_item_id", "updated_at"}
        }
        returned = client.update_row(
            "promo_offer_items",
            {"offer_item_id": f"eq.{update['offer_item_id']}", "updated_at": f"eq.{update['updated_at']}"},
            payload,
        )
        if not returned:
            conflicts.append({"table": "promo_offer_items", "id": update["offer_item_id"]})
            continue
        actual = returned[0]
        mismatched_fields = {
            field: {"expected": update[field], "actual": actual.get(field)}
            for field in ("quantity", "unit_price")
            if field in update
            and str(actual.get("delivered_units" if field == "quantity" else field))
            != str(update[field])
        }
        if mismatched_fields:
            mismatches.append({"offer_item_id": update["offer_item_id"], "fields": mismatched_fields})
        else:
            applied.append(int(update["offer_item_id"]))
    return {"applied": applied, "conflicts": conflicts, "mismatches": mismatches}


def _project_rows(rows: list[dict[str, Any]], plan: SemanticRepairPlan) -> list[dict[str, Any]]:
    projected = [{**row, "promo_offer_items": [dict(item) for item in row.get("promo_offer_items", [])]} for row in rows]
    by_id = {row.get("id"): row for row in projected}
    for update in plan.master_updates:
        row = by_id.get(update.get("id"))
        if row:
            row.update({key: value for key, value in update.items() if key not in {"id", "updated_at"}})
    item_by_id = {
        item.get("offer_item_id"): item
        for row in projected
        for item in row.get("promo_offer_items", [])
    }
    for update in plan.item_updates:
        item = item_by_id.get(update.get("offer_item_id"))
        if item:
            item.update({key: value for key, value in update.items() if key not in {"offer_item_id", "updated_at"}})
    return projected


def _service_mapping_review(rows: list[dict[str, Any]], services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    service_by_id = {int(row["service_id"]): row for row in services if row.get("service_id") is not None}
    hints = (
        "botox", "dysport", "xeomin", "daxxify", "jeuveau", "sculptra", "juvederm",
        "restylane", "radiesse", "skinvive", "coolsculpting", "thermage", "morpheus",
        "microneedling", "hydrafacial", "kybella",
    )
    findings: list[dict[str, Any]] = []
    for row in rows:
        raw = str(row.get("offer_raw_text") or "")
        raw_lower = raw.casefold()
        mentioned = [hint for hint in hints if re.search(rf"\b{re.escape(hint)}\b", raw_lower)]
        if len(set(mentioned)) != 1:
            continue
        for item in row.get("promo_offer_items", []):
            service = service_by_id.get(item.get("service_id"))
            service_name = str(service.get("service_name") if service else "").casefold()
            if mentioned[0] not in service_name:
                findings.append(
                    {
                        "offer_id": row.get("id"),
                        "offer_item_id": item.get("offer_item_id"),
                        "service_id": item.get("service_id"),
                        "service_name": service.get("service_name") if service else None,
                        "raw_text": raw,
                        "mentioned_service": mentioned[0],
                    }
                )
    return findings


def _fixed_price_records(rows: list[dict[str, Any]], plan: SemanticRepairPlan) -> list[dict[str, Any]]:
    by_id = {row.get("id"): row for row in rows}
    records: list[dict[str, Any]] = []
    for update in plan.master_updates:
        row = by_id.get(update.get("id"), {})
        records.append({"table": "promo_offer_master", "id": update.get("id"), "raw_text": row.get("offer_raw_text", ""), "patch": {key: value for key, value in update.items() if key not in {"id", "updated_at"}}})
    item_by_id = {
        item.get("offer_item_id"): (row.get("id"), row.get("offer_raw_text", ""))
        for row in rows
        for item in row.get("promo_offer_items", [])
    }
    for update in plan.item_updates:
        offer_id, raw = item_by_id.get(update.get("offer_item_id"), (None, ""))
        patch = {key: value for key, value in update.items() if key not in {"offer_item_id", "updated_at", "quantity"}}
        if patch:
            records.append({"table": "promo_offer_items", "id": update.get("offer_item_id"), "offer_id": offer_id, "raw_text": raw, "patch": patch})
    return records


def main() -> int:
    args = parse_args()
    rows = load_rows(reader())
    plan = build_semantic_repair_plan(rows)
    # clinic_services is intentionally hidden from public roles; use the
    # controlled server-side credential for this read-only mapping review.
    services = fetch_all_rows(writer(), "clinic_services", SERVICE_SELECT, page_size=1000, order="service_id.asc")
    projected = _project_rows(rows, plan)
    audit = audit_offer_prices(projected)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry_run",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "offer_count": len(rows),
            "master_updates": len(plan.master_updates),
            "item_updates": len(plan.item_updates),
            "quantity_updates": sum(1 for update in plan.item_updates if "quantity" in update),
            "review_cases": len(plan.review_cases),
            "projected_semantic_qa_issues": len(audit.issues),
        },
        "fixed_price_records": _fixed_price_records(rows, plan),
        "fixed_quantity_records": [update for update in plan.item_updates if "quantity" in update],
        "service_mapping_review": _service_mapping_review(rows, services),
        "remaining_price_outliers": [issue.as_row() for issue in audit.issues],
        "review_cases": plan.review_cases,
        "apply_result": {},
    }

    if args.apply:
        write_client = writer()
        master_result = _apply_master_updates(write_client, plan.master_updates)
        # Master triggers may touch item.updated_at/unit_price. Re-read before
        # applying item patches so optimistic locking reflects live state.
        fresh_rows = load_rows(reader())
        fresh_plan = build_semantic_repair_plan(fresh_rows)
        item_result = _apply_item_updates(write_client, fresh_plan.item_updates)
        result["apply_result"] = {"master": master_result, "items": item_result}
        result["summary"]["applied_master_updates"] = len(master_result["applied"])
        result["summary"]["applied_item_updates"] = len(item_result["applied"])
        result["summary"]["write_conflicts"] = len(master_result["conflicts"]) + len(item_result["conflicts"])
        result["summary"]["write_mismatches"] = len(item_result["mismatches"])

    path = output_dir / f"promo_offer_price_repair_{stamp}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(path), **result["summary"], "mode": result["mode"]}, ensure_ascii=False))
    return 0 if not args.apply or not result["apply_result"].get("items", {}).get("conflicts") else 2


if __name__ == "__main__":
    raise SystemExit(main())
