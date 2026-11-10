from __future__ import annotations

from typing import Any

from utils.offer_conditions_backfill_plan import (
    MEMBERSHIP_SELECT,
    PLAN_VERSION,
    PROMOTION_SELECT,
    _fetch_all,
    _in_filter,
)
from utils.supabase_rest import SupabaseRestClient


def _current_sources(
    client: SupabaseRestClient,
    rows: list[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    refs = {(row.get("source_type"), row.get("source_id")) for row in rows}
    promotion_ids = {int(source_id) for source_type, source_id in refs if source_type == "promotion" and source_id is not None}
    membership_ids = {int(source_id) for source_type, source_id in refs if source_type == "membership" and source_id is not None}
    current: dict[tuple[str, int], dict[str, Any]] = {}
    if promotion_ids:
        for row in _fetch_all(
            client,
            "clinic_promotions",
            PROMOTION_SELECT,
            filters={"promotion_id": _in_filter(promotion_ids)},
        ):
            current[("promotion", int(row["promotion_id"]))] = row
    if membership_ids:
        for row in _fetch_all(
            client,
            "clinic_memberships",
            MEMBERSHIP_SELECT,
            filters={"plan_id": _in_filter(membership_ids)},
        ):
            current[("membership", int(row["plan_id"]))] = row
    return current


def apply_plan(
    plan: dict[str, Any],
    reader: SupabaseRestClient,
    writer: SupabaseRestClient,
) -> dict[str, Any]:
    if plan.get("plan_version") != PLAN_VERSION or plan.get("scope") != "active_conditions_null":
        raise ValueError("unsupported conditions backfill plan")
    ready = [row for row in plan.get("rows", []) if row.get("status") == "ready"]
    current_sources = _current_sources(reader, ready)
    offer_ids = {int(row["offer_id"]) for row in ready}
    current_offers = (
        {
            int(row["id"]): row
            for row in _fetch_all(
                reader,
                "promo_offer_master",
                "id,is_active,conditions,updated_at,promotion_id,membership_plan_id",
                filters={"id": _in_filter(offer_ids)},
            )
        }
        if offer_ids
        else {}
    )
    result: dict[str, Any] = {"applied": [], "conflicts": [], "review_rows": len(plan.get("rows", [])) - len(ready)}
    for row in sorted(ready, key=lambda item: int(item["offer_id"])):
        offer_id = int(row["offer_id"])
        source_key = (row.get("source_type"), row.get("source_id"))
        source = current_sources.get(source_key)
        if source is None or source.get("updated_at") != row.get("source_updated_at"):
            result["conflicts"].append({"offer_id": offer_id, "reason": "source_changed_since_plan"})
            continue
        current = current_offers.get(offer_id)
        if (
            current is None
            or current.get("is_active") is not True
            or current.get("conditions") is not None
            or current.get("updated_at") != row.get("offer_updated_at")
        ):
            result["conflicts"].append({"offer_id": offer_id, "reason": "offer_changed_since_plan"})
            continue
        updated = writer.update_row(
            "promo_offer_master",
            {
                "id": f"eq.{offer_id}",
                "is_active": "eq.true",
                "conditions": "is.null",
                "updated_at": f"eq.{row['offer_updated_at']}",
            },
            {"conditions": row["conditions"]},
        )
        if not updated:
            result["conflicts"].append({"offer_id": offer_id, "reason": "offer_changed_since_plan"})
            continue
        if updated[0].get("conditions") != row["conditions"]:
            result["conflicts"].append({"offer_id": offer_id, "reason": "write_verification_failed"})
            continue
        result["applied"].append(offer_id)
    all_ids = {int(row["offer_id"]) for row in plan.get("rows", [])}
    after_rows = (
        _fetch_all(
            reader,
            "promo_offer_master",
            "id,is_active,conditions,updated_at",
            filters={"id": _in_filter(all_ids)},
        )
        if all_ids
        else []
    )
    after_by_id = {int(row["id"]): row for row in after_rows}
    applied_set = set(result["applied"])
    ready_mismatches: list[dict[str, Any]] = []
    review_mismatches: list[dict[str, Any]] = []
    for row in plan.get("rows", []):
        current = after_by_id.get(int(row["offer_id"]))
        if current is None:
            (ready_mismatches if row.get("status") == "ready" else review_mismatches).append(
                {"offer_id": int(row["offer_id"]), "reason": "offer_missing_on_readback"}
            )
            continue
        if row.get("status") == "ready" and int(row["offer_id"]) in applied_set:
            if current.get("conditions") != row.get("conditions"):
                ready_mismatches.append(
                    {"offer_id": int(row["offer_id"]), "reason": "ready_conditions_mismatch"}
                )
        elif row.get("status") == "review" and current.get("conditions") is not None:
            review_mismatches.append(
                {"offer_id": int(row["offer_id"]), "reason": "review_conditions_changed"}
            )
    result["readback"] = {
        "targets": len(all_ids),
        "ready_mismatches": ready_mismatches,
        "review_mismatches": review_mismatches,
    }
    return result
