from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from utils.clinic_promotions_db import upsert_promotion
from utils.clinic_service_extraction import upsert_extracted_service
from utils.recent_raw_extraction import (
    build_promotion_content,
    validate_promotion,
)
from utils.rendered_visibility_gate import filter_visible_promotion_content
from utils.schema_contract import TABLE_CLINIC_MEMBERSHIPS, TABLE_CLINIC_PROMOTIONS


def persist_service_item(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
) -> dict[str, Any]:
    return upsert_extracted_service(
        client,
        business_id=business_id,
        item=item,
        source_url=source_url,
        evidence=evidence,
    )


def persist_membership_item(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
    rendered_visibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from utils.membership_evidence import prepare_membership_write
    decision = prepare_membership_write(item, evidence, rendered_visibility)
    if not decision["accepted"]:
        return {"accepted": False, "reason": decision["reason"], "action": "skipped"}
    row = {
        "business_id": business_id,
        "membership_name": str(item.get("membership_name") or "").strip(),
        "membership_price": decision["membership_price"],
        "billing_period": item.get("billing_period"),
        "minimum_commitment_months": item.get("minimum_commitment_months"),
        "benefits": decision["benefits"],
        "source_url": str(source_url or "").strip().rstrip("/"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    matches = client.fetch_rows(TABLE_CLINIC_MEMBERSHIPS,
        "plan_id,business_id,membership_name,membership_price,benefits,billing_period,minimum_commitment_months,is_offer_extracted,source_url,updated_at",
        filters={"business_id": f"eq.{business_id}", "source_url": f"eq.{row['source_url']}"})
    matches = [old for old in matches if str(old.get("membership_name") or "").strip().casefold() == row["membership_name"].casefold()]
    if len(matches) > 1:
        return {"accepted": False, "reason": "ambiguous_membership_identity", "action": "quarantined"}
    # is_offer_extracted 只在新行或权益变化时置为 false（与 page_extraction.MEMBERSHIP_UPSERT 同一规则），
    # 之后由 extract-offers / link-services 在权益转成 offer 且服务映射完整后置为 true。
    if matches:
        old = matches[0]
        if decision["benefits"] is None:
            row.pop("benefits")
        elif decision["benefits"] != old.get("benefits"):
            row["is_offer_extracted"] = False
        if all(old.get(key) == value for key, value in row.items() if key != "updated_at"):
            return {"accepted": True, "action": "unchanged", "plan_id": int(old["plan_id"])}
        filters = {"plan_id": f"eq.{old['plan_id']}"}
        if old.get("updated_at"):
            filters["updated_at"] = f"eq.{old['updated_at']}"
        inserted = client.update_row(TABLE_CLINIC_MEMBERSHIPS, filters, row)
        if len(inserted) != 1:
            return {"accepted": False, "reason": "membership_write_conflict", "action": "quarantined"}
        action = "updated"
    else:
        row["is_offer_extracted"] = False
        inserted = client.insert_rows(TABLE_CLINIC_MEMBERSHIPS, [row])
        action = "inserted"
    plan_id = int(inserted[0]["plan_id"]) if action == "inserted" else int(matches[0]["plan_id"])
    actual = client.fetch_rows(TABLE_CLINIC_MEMBERSHIPS, ",".join(row),
                              filters={"plan_id": f"eq.{plan_id}"}, limit=1)
    if len(actual) != 1 or any(actual[0].get(key) != value for key, value in row.items() if key != "updated_at"):
        return {"accepted": False, "reason": "membership_readback_mismatch", "action": "quarantined", "plan_id": plan_id}
    return {
        "accepted": True,
        "action": action,
        "plan_id": plan_id,
    }


def persist_promotion_item(
    client: Any,
    *,
    business_id: int,
    item: dict[str, Any],
    source_url: str,
    evidence: str,
    rendered_visibility: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    expanded = dict(item)
    expanded["promotion_content"] = build_promotion_content(item, evidence)
    visibility = filter_visible_promotion_content(
        expanded["promotion_content"], rendered_visibility
    )
    if not visibility.accepted:
        return {"accepted": False, "reason": visibility.reason, "action": "skipped"}
    expanded["promotion_content"] = list(visibility.content)
    decision = validate_promotion(expanded, evidence)
    if not decision.accepted:
        return {"accepted": False, "reason": decision.reason, "action": "skipped"}
    promotion_id = upsert_promotion(client, business_id=business_id, source_url=source_url)
    payload = {
        "promotion_title": str(expanded.get("promotion_title") or "Promotion"),
        "promotion_content": expanded.get("promotion_content") or [],
        "campaign_start_date": expanded.get("campaign_start_date"),
        "campaign_end_date": expanded.get("campaign_end_date"),
        "is_active": True,
    }
    current = client.fetch_rows(
        TABLE_CLINIC_PROMOTIONS,
        ",".join(payload),
        filters={"promotion_id": f"eq.{promotion_id}"},
        limit=1,
    )
    if current and all(current[0].get(key) == value for key, value in payload.items()):
        return {"accepted": True, "action": "unchanged", "promotion_id": int(promotion_id)}
    client.update_row(
        TABLE_CLINIC_PROMOTIONS,
        {"promotion_id": f"eq.{promotion_id}"},
        {**payload, "updated_at": datetime.now(timezone.utc).isoformat()},
    )
    return {"accepted": True, "action": "upserted", "promotion_id": int(promotion_id)}
