"""Deterministic repair planner + controlled executor for extraction quality."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from utils.extraction_quality_audit import _infer_commitment_months
from utils.recent_raw_extraction import _mostly_low_quality
from utils.offer_fingerprint import compute_offer_fingerprint
from utils.offer_extraction_llm import canonicalize_service_name
from utils.promo_offer_repair import build_all_repair_plans
from utils.schema_contract import (
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_CLINIC_PROMOTIONS,
    TABLE_CLINIC_SERVICES,
    TABLE_PROMO_OFFER_MASTER,
    offer_source_url,
)

_PERCENT_IN_TEXT = re.compile(r"\d+(?:\.\d+)?\s*%")


def plan_membership_commitment_repairs(
    memberships: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for row in memberships:
        benefits = row.get("benefits") or []
        inferred = _infer_commitment_months(benefits)
        if inferred and not row.get("minimum_commitment_months"):
            actions.append(
                {
                    "batch": "membership_commitment",
                    "action": "update",
                    "table": TABLE_CLINIC_MEMBERSHIPS,
                    "id": row.get("plan_id"),
                    "fields": {"minimum_commitment_months": inferred},
                    "reason": f"backfill_commitment_from_benefits:{inferred}",
                }
            )
    return actions


def plan_service_canonical_repairs(
    services: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for row in services:
        raw = str(row.get("service_name_raw") or "").strip()
        if row.get("service_name") != "Others" or not raw:
            continue
        canonical = canonicalize_service_name(raw, raw)
        target = canonical if canonical and canonical != "Others" else raw
        actions.append(
            {
                "batch": "service_canonical",
                "action": "update",
                "table": TABLE_CLINIC_SERVICES,
                "id": row.get("service_id"),
                "fields": {"service_name": target},
                "reason": f"remap_others_to:{target}",
            }
        )
    return actions


def plan_promotion_content_repairs(
    promotions: Sequence[Mapping[str, Any]],
    *,
    scrape_markdown_by_url: Mapping[str, str],
) -> List[Dict[str, Any]]:
    from utils.recent_raw_extraction import build_promotion_content, validate_promotion

    actions: List[Dict[str, Any]] = []
    for row in promotions:
        if row.get("promotion_content") and not _mostly_low_quality(row.get("promotion_content") or []):
            continue
        url = str(row.get("source_url") or "").strip().rstrip("/").lower()
        markdown = scrape_markdown_by_url.get(url) or ""
        if not markdown:
            continue
        item = {
            "promotion_title": row.get("promotion_title"),
            "promotion_content": row.get("promotion_content") or [],
        }
        expanded = build_promotion_content(item, markdown)
        candidate = {**item, "promotion_content": expanded}
        if not validate_promotion(candidate, markdown).accepted:
            continue
        actions.append(
            {
                "batch": "promotion_content",
                "action": "update",
                "table": TABLE_CLINIC_PROMOTIONS,
                "id": row.get("promotion_id"),
                "fields": {"promotion_content": expanded},
                "reason": "backfill_from_scrape_markdown",
            }
        )
    return actions


def plan_offer_quality_repairs(
    offers: Sequence[Mapping[str, Any]],
    *,
    promotions: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    promo_by_id = {row["promotion_id"]: row for row in promotions if row.get("promotion_id") is not None}
    actions: List[Dict[str, Any]] = []
    for row in offers:
        if not row.get("is_active"):
            continue
        row_id = row.get("id")
        raw = str(row.get("offer_raw_text") or "")
        promo = promo_by_id.get(row.get("promotion_id")) or {}
        source_url = offer_source_url({"clinic_promotions": promo, **dict(row)})

        if re.search(r"\$\d+(?:\.\d+)?\s*per month", raw, re.I):
            actions.append(
                {
                    "batch": "offer_scope",
                    "action": "update",
                    "table": TABLE_PROMO_OFFER_MASTER,
                    "id": row_id,
                    "fields": {"is_active": False},
                    "reason": "membership_fee_not_promo_offer",
                }
            )
            continue

        fields: Dict[str, Any] = {}
        regular = row.get("regular_price")
        discount = row.get("discount_price")
        if regular is not None and discount is not None and float(regular) == float(discount):
            fields["discount_price"] = None
            fields["discount_amount"] = row.get("discount_amount")
        if row.get("discount_percent") is not None and not _PERCENT_IN_TEXT.search(raw):
            fields["discount_percent"] = None
        if fields:
            actions.append(
                {
                    "batch": "offer_pricing",
                    "action": "update",
                    "table": TABLE_PROMO_OFFER_MASTER,
                    "id": row_id,
                    "fields": fields,
                    "reason": "clear_derived_or_equal_discount_fields",
                }
            )

        fp = str(row.get("offer_fingerprint") or "")
        if len(fp) == 32:
            service_name = canonicalize_service_name(raw, raw) or "Offer"
            new_fp = compute_offer_fingerprint(
                source_url=source_url,
                service_name=service_name,
                regular_price=row.get("regular_price"),
                discount_price=row.get("discount_price"),
                offer_raw_text=raw,
            )
            actions.append(
                {
                    "batch": "offer_fingerprint",
                    "action": "update",
                    "table": TABLE_PROMO_OFFER_MASTER,
                    "id": row_id,
                    "fields": {"offer_fingerprint": new_fp},
                    "reason": "upgrade_legacy_fingerprint",
                }
            )
    return actions


def build_extraction_repair_plan(
    *,
    services: Sequence[Mapping[str, Any]],
    memberships: Sequence[Mapping[str, Any]],
    promotions: Sequence[Mapping[str, Any]],
    offers: Sequence[Mapping[str, Any]],
    master_rows: Sequence[Mapping[str, Any]],
    staging_rows: Sequence[Mapping[str, Any]],
    scrape_markdown_by_url: Optional[Mapping[str, str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    plans = build_all_repair_plans(
        offers,
        master_rows=master_rows,
        staging_rows=staging_rows,
        promotion_rows=promotions,
    )
    plans["membership_commitment"] = plan_membership_commitment_repairs(memberships)
    plans["service_canonical"] = plan_service_canonical_repairs(services)
    plans["promotion_content"] = plan_promotion_content_repairs(
        promotions,
        scrape_markdown_by_url=scrape_markdown_by_url or {},
    )
    plans["offer_quality"] = plan_offer_quality_repairs(offers, promotions=promotions)
    return plans


def apply_repair_actions(
    client: Any,
    actions: Sequence[Mapping[str, Any]],
    *,
    dry_run: bool = True,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for action in actions:
        table = str(action.get("table") or TABLE_PROMO_OFFER_MASTER)
        row_id = action.get("id")
        fields = dict(action.get("fields") or {})
        if action.get("action") == "delete":
            result = {"dry_run": dry_run, **dict(action), "applied": False}
            if not dry_run and row_id is not None:
                client.delete_rows(table, {f"{_pk_column(table)}": f"eq.{row_id}"})
                result["applied"] = True
            results.append(result)
            continue
        if not fields:
            continue
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        result = {"dry_run": dry_run, **dict(action), "applied": False}
        if not dry_run and row_id is not None:
            client.update_row(table, {_pk_column(table): f"eq.{row_id}"}, fields)
            result["applied"] = True
        results.append(result)
    return results


def _pk_column(table: str) -> str:
    return {
        TABLE_CLINIC_SERVICES: "service_id",
        TABLE_CLINIC_MEMBERSHIPS: "plan_id",
        TABLE_CLINIC_PROMOTIONS: "promotion_id",
        TABLE_PROMO_OFFER_MASTER: "id",
    }.get(table, "id")
