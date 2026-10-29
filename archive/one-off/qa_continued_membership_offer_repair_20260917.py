#!/usr/bin/env python3
"""Final read-only QA for the continued clinic membership repair."""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".firecrawl" / "clinic_memberships_plan_20260917" / "continued_repair_qa_20260917.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.schema_contract import OFFER_MASTER_SELECT
from utils.supabase_access import fetch_all_rows, load_supabase_client


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def main() -> int:
    client = load_supabase_client(ROOT)
    memberships = fetch_all_rows(client, "clinic_memberships", "plan_id,business_id,membership_name,benefits,is_offer_extracted,is_ocr_required,need_browser,updated_at", order="plan_id.asc")
    services = fetch_all_rows(client, "clinic_services", "service_id,business_id,service_name,source_url", order="service_id.asc")
    offers = fetch_all_rows(client, "promo_offer_master", OFFER_MASTER_SELECT, filters={"membership_plan_id": "not.is.null"}, order="id.asc")
    items = fetch_all_rows(client, "promo_offer_items", "offer_item_id,offer_id,service_id,item_name,delivered_units,unit_price", order="offer_item_id.asc")
    membership_by_id = {int(row["plan_id"]): row for row in memberships}
    service_by_id = {int(row["service_id"]): row for row in services}
    offer_by_id = {int(row["id"]): row for row in offers}
    items_by_offer = defaultdict(list)
    for item in items:
        items_by_offer[int(item["offer_id"])].append(item)

    active = [row for row in offers if row.get("is_active")]
    active_items = [item for item in items if int(item["offer_id"]) in {int(row["id"]) for row in active}]
    empty_conditions = [int(row["id"]) for row in active if not row.get("conditions")]
    active_without_items = [int(row["id"]) for row in active if not items_by_offer[int(row["id"])] ]
    active_null_service_items = [int(item["offer_item_id"]) for item in active_items if item.get("service_id") is None]
    item_name_mismatches = [int(item["offer_item_id"]) for item in active_items if item.get("service_id") is not None and service_by_id.get(int(item["service_id"]), {}).get("service_name") != item.get("item_name")]
    cross_business = []
    for item in active_items:
        service = service_by_id.get(int(item.get("service_id") or 0))
        offer = offer_by_id.get(int(item["offer_id"]))
        if service and offer and int(service["business_id"]) != int(offer["business_id"]):
            cross_business.append(int(item["offer_item_id"]))
    active_missing_unit_price = [int(item["offer_item_id"]) for item in active_items if offer_by_id[int(item["offer_id"])].get("price_model") == "per_unit" and offer_by_id[int(item["offer_id"])].get("discount_price") is not None and item.get("unit_price") is None]
    active_fingerprints = defaultdict(list)
    for row in active:
        active_fingerprints[str(row.get("offer_fingerprint"))].append(int(row["id"]))
    duplicate_fingerprints = [ids for ids in active_fingerprints.values() if len(ids) > 1]

    semantic = defaultdict(list)
    for row in active:
        plan_id = int(row["membership_plan_id"])
        offer_items = items_by_offer[int(row["id"])]
        if not offer_items:
            continue
        # A multi-service package and a single-service benefit can share the
        # same percentage while remaining materially different.  Only flag
        # exact same service-id/quantity/unit-price sets with the same terms.
        item_set = tuple(sorted(
            (int(item.get("service_id") or 0), item.get("delivered_units"), item.get("unit_price"))
            for item in offer_items
        ))
        key = (plan_id, item_set, row.get("price_model"), row.get("regular_price"), row.get("discount_price"), row.get("discount_percent"), row.get("discount_amount"))
        semantic[key].append(int(row["id"]))
    semantic_duplicates = [ids for ids in semantic.values() if len(ids) > 1]

    def active_service_names(plan_id: int) -> List[str]:
        names = []
        for row in active:
            if int(row.get("membership_plan_id") or 0) != plan_id:
                continue
            names.extend(service_by_id[int(item["service_id"])] ["service_name"] for item in items_by_offer[int(row["id"])] if item.get("service_id") is not None and int(item["service_id"]) in service_by_id)
        return sorted(set(names), key=norm)

    expected_425_426 = ["BBL Photofacial", "Halo Laser", "Hydrafacial", "Morpheus8"]
    expected_697 = ["Body Contouring", "Facial", "IPL Photofacial", "SkinPen Microneedling"]
    checks = {
        "duplicate_old_offer_ids_inactive": all(not offer_by_id.get(offer_id, {}).get("is_active") for offer_id in (7830, 8751, 7821, 8833, 8835, 7878)),
        "425_has_expected_services": all(name in active_service_names(425) for name in expected_425_426),
        "426_has_expected_services": all(name in active_service_names(426) for name in expected_425_426),
        "697_has_expected_services": all(name in active_service_names(697) for name in expected_697),
        "700_has_facial": "Facial" in active_service_names(700),
        "483_484_extraction_false": membership_by_id[483]["is_offer_extracted"] is False and membership_by_id[484]["is_offer_extracted"] is False,
        "699_has_pearl_fractional": "Pearl Fractional" in active_service_names(699),
        "699_extraction_true": membership_by_id[699]["is_offer_extracted"] is True,
        "323_rollover_added": any("roll over per 6-month period" in str(value) for value in membership_by_id[323].get("benefits") or []),
        "461_462_process_notes_removed": membership_by_id[461].get("benefits") == [] and membership_by_id[462].get("benefits") == [],
        "461_462_review_flags_preserved": all(membership_by_id[pid].get("is_ocr_required") is True and membership_by_id[pid].get("need_browser") is True and membership_by_id[pid].get("is_offer_extracted") is False for pid in (461, 462)),
        "pearl_fractional_in_taxonomy": any(norm(row["service_name"]) == "pearl fractional" for row in services),
        "structural_gates_clear": not any((empty_conditions, active_without_items, active_null_service_items, item_name_mismatches, cross_business, active_missing_unit_price, duplicate_fingerprints, semantic_duplicates)),
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "membership_total": len(memberships),
        "active_membership_offer_total": sum(1 for row in active if row.get("membership_plan_id") is not None),
        "true_extracted_zero_active": sorted(int(row["plan_id"]) for row in memberships if row.get("is_offer_extracted") and not any(int(offer.get("membership_plan_id") or 0) == int(row["plan_id"]) for offer in active)),
        "target_active_service_names": {str(pid): active_service_names(pid) for pid in (425, 426, 697, 700)},
        "structural_issues": {
            "empty_conditions": empty_conditions,
            "active_without_items": active_without_items,
            "active_null_service_items": active_null_service_items,
            "item_name_mismatches": item_name_mismatches,
            "cross_business": cross_business,
            "active_per_unit_missing_unit_price": active_missing_unit_price,
            "duplicate_fingerprints": duplicate_fingerprints,
            "semantic_duplicates": semantic_duplicates,
        },
        "checks": checks,
        "all_checks_pass": all(checks.values()),
        "review_queue": [483, 484, 461, 462],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_checks_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
