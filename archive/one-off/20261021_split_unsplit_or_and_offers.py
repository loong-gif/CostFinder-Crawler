#!/usr/bin/env python3
"""Dry-run / apply repair for active offers failing J_unsplit_or_and_composition."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.batch_gate_checks import CHECKS, linked_query, run_batch_gate
from utils.extraction_persist_offer import persist_promo_offer
from utils.offer_evidence_validator import validate_offer_evidence
from utils.offer_purchase_semantics import normalize_purchase_offers
from utils.promo_offer_items_db import fetch_items_for_offer, upsert_offer_items
from utils.schema_contract import TABLE_PROMO_OFFER_MASTER
from utils.supabase_access import load_supabase_client

OUT = ROOT / "one-off/outputs/split_unsplit_or_and_20261021"
J_SQL = CHECKS["J_unsplit_or_and_composition"]

MASTER_SELECT = (
    "id,business_id,promotion_id,membership_plan_id,offer_raw_text,is_active,updated_at,"
    "regular_price,discount_price,discount_percent,discount_amount,price_model,is_package,"
    "is_new_customer_required,is_membership_required,conditions"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _service_name(client, service_id: int | None) -> str | None:
    if service_id is None:
        return None
    rows = client.fetch_rows("clinic_services", "service_name", filters={"service_id": f"eq.{int(service_id)}"}, limit=1)
    return str(rows[0]["service_name"]) if rows else None


def _conditions_for_validate(stored: list | None) -> list:
    """Persisted text[] conditions lack extraction evidence; reuse requirement text."""
    if not stored:
        return []
    out = []
    for entry in stored:
        if isinstance(entry, str) and entry.strip():
            text = entry.strip()
            out.append({"requirement": text, "evidence": text})
        elif isinstance(entry, dict):
            out.append(entry)
    return out


def master_to_offer(master: dict, items: list[dict]) -> dict:
    return {
        "offer_raw_text": master.get("offer_raw_text"),
        "regular_price": master.get("regular_price"),
        "discount_price": master.get("discount_price"),
        "discount_percent": master.get("discount_percent"),
        "discount_amount": master.get("discount_amount"),
        "price_model": master.get("price_model") or ("per_unit" if re.search(r"(?:/\s*|\bper\s+)unit\b", str(master.get("offer_raw_text") or ""), re.I) else "total"),
        "is_membership_required": bool(master.get("is_membership_required")),
        "is_new_customer_required": bool(master.get("is_new_customer_required")),
        "conditions": _conditions_for_validate(master.get("conditions")),
        "purchase_type": "independent_options" if len(items) > 1 else "single",
        "is_package": bool(master.get("is_package")),
        "items": [
            {
                "item_name": item.get("item_name"),
                "service_name": _service_name_from_item(item),
                "quantity": item.get("delivered_units", item.get("quantity")),
                "unit_price": item.get("unit_price"),
                "unit_type": None,
                "service_area": None,
            }
            for item in items
        ],
    }


def _service_name_from_item(item: dict) -> str | None:
    return item.get("service_name") or item.get("item_name")


def _evidence_text(master: dict) -> str:
    raw = str(master.get("offer_raw_text") or "")
    extra = [str(line).strip() for line in (master.get("conditions") or []) if str(line).strip()]
    return raw if not extra else raw + "\n" + "\n".join(extra)


def classify(client, master: dict, items: list[dict]) -> dict:
    offer = master_to_offer(master, items)
    raw = str(master.get("offer_raw_text") or "")
    evidence = _evidence_text(master)
    normalized, reviews = normalize_purchase_offers([offer], evidence)
    if reviews and all(r.get("reason") == "unresolved_purchase_choices" for r in reviews):
        normalized, reviews = normalize_purchase_offers(normalized or [offer], evidence)
    rejected = [r for r in reviews if r.get("reason") != "generic_offer_scope"]
    entry = {
        "offer_id": int(master["id"]),
        "business_id": int(master["business_id"]),
        "promotion_id": master.get("promotion_id"),
        "membership_plan_id": master.get("membership_plan_id"),
        "updated_at": master.get("updated_at"),
        "offer_raw_text": raw[:240],
        "item_count": len(items),
        "is_package": master.get("is_package"),
    }
    if rejected:
        entry.update(status="review", action="none", reason=rejected[0]["reason"])
        return entry
    if len(normalized) > 1:
        stored_conditions = list(master.get("conditions") or [])
        branches = []
        for branch in normalized:
            repaired = dict(branch)
            if stored_conditions:
                repaired["conditions"] = _conditions_for_validate(stored_conditions)
            branches.append(repaired)
        entry.update(status="ready", action="split_or", branch_count=len(branches), branches=branches)
        return entry
    one = normalized[0] if normalized else offer
    if one.get("is_package") and len(one.get("items") or []) > 1 and not master.get("is_package"):
        decision = validate_offer_evidence(one, evidence)
        if not decision.accepted:
            entry.update(status="review", action="none", reason=decision.reason or "evidence_failed")
            return entry
        repaired = dict(decision.offer)
        stored_conditions = list(master.get("conditions") or [])
        if stored_conditions:
            repaired["conditions"] = _conditions_for_validate(stored_conditions)
        entry.update(status="ready", action="mark_package", target=repaired)
        return entry
    entry.update(status="review", action="none", reason="no_automatic_repair")
    return entry


def build_plan(client, *, business_id: int | None, limit: int | None, offer_ids: list[int] | None = None) -> dict:
    targets = linked_query(J_SQL, root=ROOT)
    gate_ids = [int(row["id"]) for row in targets]
    merged = list(dict.fromkeys((offer_ids or []) + gate_ids))
    if limit is not None:
        merged = merged[:limit]
    records = []
    extra_ids = set(offer_ids or [])
    for offer_id in merged:
        rows = client.fetch_rows(TABLE_PROMO_OFFER_MASTER, MASTER_SELECT, filters={"id": f"eq.{offer_id}"}, limit=1)
        if not rows:
            continue
        if not rows[0].get("is_active") and offer_id not in extra_ids:
            continue
        master = rows[0]
        if business_id is not None and int(master["business_id"]) != business_id:
            continue
        items = fetch_items_for_offer(client, offer_id)
        for item in items:
            if item.get("service_id") and not item.get("service_name"):
                item["service_name"] = _service_name(client, item.get("service_id"))
        entry = classify(client, master, items)
        if not master.get("is_active"):
            entry["recover_inactive"] = True
        records.append(entry)
    return {
        "generated_at": _now(),
        "gate": "J_unsplit_or_and_composition",
        "target_count": len(merged),
        "records": records,
        "summary": {
            "ready": sum(1 for r in records if r["status"] == "ready"),
            "review": sum(1 for r in records if r["status"] == "review"),
            "split_or": sum(1 for r in records if r.get("action") == "split_or"),
            "mark_package": sum(1 for r in records if r.get("action") == "mark_package"),
        },
    }


def _source_url(client, master: dict) -> str | None:
    if master.get("promotion_id"):
        rows = client.fetch_rows(
            "clinic_promotions",
            "source_url",
            filters={"promotion_id": f"eq.{master['promotion_id']}"},
            limit=1,
        )
        return str(rows[0]["source_url"]).rstrip("/") if rows else None
    if master.get("membership_plan_id"):
        rows = client.fetch_rows(
            "clinic_memberships",
            "source_url",
            filters={"plan_id": f"eq.{master['membership_plan_id']}"},
            limit=1,
        )
        return str(rows[0]["source_url"]).rstrip("/") if rows else None
    return None


def apply_plan(client, plan: dict) -> dict:
    results = []
    for record in plan["records"]:
        if record["status"] != "ready":
            results.append({**record, "apply_status": "skipped", "apply_reason": record.get("reason")})
            continue
        offer_id = record["offer_id"]
        rows = client.fetch_rows(TABLE_PROMO_OFFER_MASTER, MASTER_SELECT, filters={"id": f"eq.{offer_id}"}, limit=1)
        if not rows:
            results.append({**record, "apply_status": "conflict", "apply_reason": "missing_master"})
            continue
        master = rows[0]
        recover = bool(record.get("recover_inactive"))
        if master.get("is_active") and master.get("updated_at") != record.get("updated_at"):
            results.append({**record, "apply_status": "conflict", "apply_reason": "updated_at_changed"})
            continue
        if not master.get("is_active") and not recover:
            results.append({**record, "apply_status": "conflict", "apply_reason": "already_inactive"})
            continue
        source_url = _source_url(client, master)
        if not source_url:
            results.append({**record, "apply_status": "review", "apply_reason": "missing_source_url"})
            continue
        raw = str(master.get("offer_raw_text") or "")
        evidence = _evidence_text(master)
        filters = {"id": f"eq.{offer_id}"}
        if master.get("updated_at"):
            filters["updated_at"] = f"eq.{master['updated_at']}"
        if record["action"] == "mark_package":
            target = record["target"]
            patch = {
                "is_package": True,
                "price_model": target.get("price_model") or "total",
                "is_active": True,
                "updated_at": _now(),
            }
            reactivated = client.update_row(
                TABLE_PROMO_OFFER_MASTER,
                {"id": f"eq.{offer_id}"},
                patch,
            )
            if len(reactivated) != 1:
                results.append({**record, "apply_status": "conflict", "apply_reason": "reactivate_failed"})
                continue
            from utils.clinic_service_extraction import attach_service_ids_to_items
            from utils.extraction_persist_pricing import build_master_from_offer
            from utils.promo_offer_items_db import is_blanket_offer_text

            if master.get("promotion_id") is None and master.get("membership_plan_id") is None:
                results.append({**record, "apply_status": "review", "apply_reason": "missing_parent_id"})
                continue
            _, item_rows = build_master_from_offer(
                target,
                business_id=int(master["business_id"]),
                promotion_id=int(master["promotion_id"]) if master.get("promotion_id") is not None else 0,
                source_url=source_url,
                membership_plan_id=int(master["membership_plan_id"]) if master.get("membership_plan_id") else None,
            )
            linked = attach_service_ids_to_items(
                client,
                business_id=int(master["business_id"]),
                items=item_rows,
                fallback_text=raw,
                is_package=True,
            )
            if not is_blanket_offer_text(raw):
                linked = [dict(item, unit_price=None) for item in linked]
            upsert_offer_items(client, offer_id, linked)
            results.append({**record, "apply_status": "applied", "apply_reason": "mark_package", "offer_id": offer_id})
            continue
        writes = []
        if master.get("promotion_id") is None and master.get("membership_plan_id") is None:
            results.append({**record, "apply_status": "review", "apply_reason": "missing_parent_id"})
            continue
        membership_id = int(master["membership_plan_id"]) if master.get("membership_plan_id") else None
        promo_id = int(master["promotion_id"]) if master.get("promotion_id") is not None else 0
        for branch in record["branches"]:
            write = persist_promo_offer(
                client,
                business_id=int(master["business_id"]),
                promotion_id=promo_id,
                source_url=source_url,
                offer=branch,
                evidence=evidence,
                membership_plan_id=membership_id,
                _skip_purchase_normalize=True,
                allow_unmapped_items=True,
                trust_persisted_offer=True,
            )
            writes.append(write)
        def _write_ok(write: dict) -> bool:
            if write.get("accepted"):
                return True
            return (
                write.get("action") == "skipped"
                and write.get("reason") == "unchanged_offer"
                and write.get("offer_id") is not None
            )

        ok = bool(writes) and all(_write_ok(write) for write in writes)
        if not ok:
            results.append(
                {**record, "apply_status": "partial", "apply_reason": "split_or_persist_failed", "writes": writes}
            )
            continue
        if master.get("is_active"):
            filters = {"id": f"eq.{offer_id}"}
            if master.get("updated_at"):
                filters["updated_at"] = f"eq.{master['updated_at']}"
            deactivated = client.update_row(
                TABLE_PROMO_OFFER_MASTER,
                filters,
                {"is_active": False, "updated_at": _now()},
            )
            if len(deactivated) != 1:
                results.append({**record, "apply_status": "partial", "apply_reason": "deactivate_after_insert_failed", "writes": writes})
                continue
        results.append(
            {
                **record,
                "apply_status": "applied",
                "apply_reason": "split_or",
                "superseded_offer_id": offer_id,
                "writes": writes,
            }
        )
    return {"applied_at": _now(), "results": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply frozen plan.json")
    parser.add_argument("--plan", action="store_true", help="Build plan (default when neither flag)")
    parser.add_argument("--business-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offer-ids", type=str, default="", help="Extra offer ids (e.g. inactive recovery)")
    parser.add_argument("--plan-path", type=Path, default=OUT / "plan.json")
    args = parser.parse_args()
    do_plan = args.plan or not args.apply
    OUT.mkdir(parents=True, exist_ok=True)
    client = load_supabase_client(ROOT)
    if do_plan:
        client.assert_write_project()
        extra = [int(part.strip()) for part in args.offer_ids.split(",") if part.strip()]
        plan = build_plan(client, business_id=args.business_id, limit=args.limit, offer_ids=extra or None)
        args.plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"plan_path": str(args.plan_path), **plan["summary"], "target_count": plan["target_count"]}, indent=2))
        return
    plan = json.loads(args.plan_path.read_text())
    client.assert_write_project()
    apply_result = apply_plan(client, plan)
    apply_path = OUT / f"apply_{apply_result['applied_at'].replace(':', '_')}.json"
    apply_path.write_text(json.dumps(apply_result, ensure_ascii=False, indent=2) + "\n")
    gate = run_batch_gate(ROOT)
    print(json.dumps({"apply_path": str(apply_path), "gate_passed": gate["passed"], "j_failing": len(gate["checks"]["J_unsplit_or_and_composition"]["failing"])}, indent=2))
    if not gate["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
