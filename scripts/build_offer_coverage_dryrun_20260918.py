#!/usr/bin/env python3
"""Build a conservative source-to-Offer coverage report for the 2026-09-18 run."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.apply_offer_plan_20260918 import fetch_all, load_client, item_list  # noqa: E402


def as_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value or "")


def source_candidate_lines(value: Any) -> list[str]:
    lines = [line.strip() for line in as_text(value).splitlines() if line.strip()]
    signal = re.compile(r"\$|%|\b(?:off|free|special|package|minimum|unit|syringe|session|treatment)\b", re.IGNORECASE)
    return [line for line in lines if signal.search(line)]


def build_report() -> dict[str, Any]:
    client = load_client()
    offers = fetch_all(client, "promo_offer_master", "id,business_id,promotion_id,membership_plan_id,is_active,is_package,price_model,regular_price,discount_price,discount_percent,discount_amount,is_new_customer_required,offer_raw_text,conditions,updated_at,promo_offer_items(offer_item_id,item_name,service_id,delivered_units,unit_price)", order="id.asc")
    services = {int(row["service_id"]): row for row in fetch_all(client, "clinic_services", "service_id,business_id,service_name", order="service_id.asc")}
    promotions = fetch_all(client, "clinic_promotions", "promotion_id,business_id,promotion_title,source_url,is_active,is_offer_extracted,is_ocr_required,requires_cf_verification,promotion_content,updated_at", order="promotion_id.asc")
    memberships = fetch_all(client, "clinic_memberships", "plan_id,business_id,membership_name,source_url,is_offer_extracted,is_ocr_required,need_browser,benefits,updated_at", order="plan_id.asc")
    offers_by_source: defaultdict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for offer in offers:
        if offer.get("promotion_id") is not None:
            offers_by_source[("promotion", int(offer["promotion_id"]))].append(offer)
        elif offer.get("membership_plan_id") is not None:
            offers_by_source[("membership", int(offer["membership_plan_id"]))].append(offer)

    source_rows: list[dict[str, Any]] = []
    for source_type, rows, id_key, content_key in (
        ("promotion", promotions, "promotion_id", "promotion_content"),
        ("membership", memberships, "plan_id", "benefits"),
    ):
        for source in rows:
            if source.get("is_active") is False:
                continue
            source_id = int(source[id_key])
            candidates = source_candidate_lines(source.get(content_key))
            current = [offer for offer in offers_by_source[(source_type, source_id)] if offer.get("is_active")]
            source_complete = source.get("is_offer_extracted") is True
            source_rows.append(
                {
                    "source_type": source_type,
                    "source_id": source_id,
                    "business_id": source.get("business_id"),
                    "source_url": source.get("source_url"),
                    "current_is_offer_extracted": source.get("is_offer_extracted"),
                    "fetch_status": "stored_source_content" if candidates or as_text(source.get(content_key)).strip() else "missing_stored_source_content",
                    "browser_required": bool(source.get("need_browser") or source.get("requires_cf_verification")),
                    "ocr_required": bool(source.get("is_ocr_required")),
                    "source_offer_candidate_count": len(candidates),
                    "source_candidate_evidence": candidates[:100],
                    "current_active_offer_count": len(current),
                    "missing_offer_count": None,
                    "stale_offer_count": None,
                    "split_required_count": sum(1 for offer in current if re.search(r"\b(?:or|any|all)\b|/", str(offer.get("offer_raw_text") or ""), re.IGNORECASE)),
                    "package_issue_count": sum(1 for offer in current if offer.get("is_package") and len(item_list(offer)) > 1 and re.search(r"\b(?:or|any|all)\b|/", str(offer.get("offer_raw_text") or ""), re.IGNORECASE)),
                    "price_issue_count": 0,
                    "terms_issue_count": sum(1 for offer in current if re.search(r"\b(?:minimum|use code|not combinable|new patient|first[- ]time)\b", str(offer.get("offer_raw_text") or ""), re.IGNORECASE) and not offer.get("conditions")),
                    "proposed_action": "KEEP" if source_complete else "REVIEW",
                    "coverage_note": "Candidate count is a conservative stored-evidence signal count, not a semantic extraction count; unresolved sources remain REVIEW.",
                }
            )

    offer_rows: list[dict[str, Any]] = []
    for offer in offers:
        if not offer.get("is_active"):
            continue
        source_type = "promotion" if offer.get("promotion_id") is not None else "membership"
        source_id = offer.get("promotion_id") or offer.get("membership_plan_id")
        offer_rows.append(
            {
                "source_type": source_type,
                "source_id": source_id,
                "source_evidence": offer.get("offer_raw_text"),
                "source_url": None,
                "business_id": offer.get("business_id"),
                "proposed_service": [item.get("item_name") for item in item_list(offer)],
                "proposed_service_id": [item.get("service_id") for item in item_list(offer)],
                "price_model": offer.get("price_model"),
                "regular_price": offer.get("regular_price"),
                "discount_price": offer.get("discount_price"),
                "discount_percent": offer.get("discount_percent"),
                "discount_amount": offer.get("discount_amount"),
                "delivered_units": [item.get("delivered_units") for item in item_list(offer)],
                "unit_price": [item.get("unit_price") for item in item_list(offer)],
                "is_package": offer.get("is_package"),
                "conditions": offer.get("conditions"),
                "current_offer_ids": [offer.get("id")],
                "action": "KEEP",
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "source_report_scope": "active promotions and memberships using stored source_content/benefits; live page retrieval is not re-run here",
        "summary": {
            "active_sources": len(source_rows),
            "unresolved_sources": sum(row["proposed_action"] == "REVIEW" for row in source_rows),
            "active_offers": len(offer_rows),
            "known_confirmed_missing_offer_count": 0,
            "semantic_missing_offer_count": None,
        },
        "source_rows": source_rows,
        "offer_rows": offer_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "offer_plan_20260918" / "offer_coverage_dry_run_20260918.json")
    args = parser.parse_args()
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), **report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
