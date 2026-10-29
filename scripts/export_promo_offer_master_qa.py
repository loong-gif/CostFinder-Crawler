#!/usr/bin/env python3
"""Export promo_offer_master in the established 35-column QA-table format."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.promo_offer_audit import audit_rows  # noqa: E402
from utils.schema_contract import OFFER_MASTER_COLUMNS  # noqa: E402
from utils.supabase_rest import SupabaseRestClient  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402


PAGE_SIZE = 1000
QA_COLUMNS = [
    "qa_status",
    "qa_notes",
    "offer_id",
    "business_id",
    "promotion_id",
    "membership_plan_id",
    "source_type",
    "source_business_id",
    "source_title",
    "Check",
    "source_url",
    "item_names",
    "offer_raw_text",
    "regular_price",
    "discount_price",
    "discount_percent",
    "discount_amount",
    "conditions",
    "service_categories",
    "delivered_quantity",
    "min_quantity",
    "discount unit_prices",
    "service_regular_prices",
    "is_membership_required",
    "is_new_customer_required",
    "source_content",
    "service_unit_types",
    "service_areas",
    "service_source_urls",
    "source_start_date",
    "source_end_date",
    "source_membership_price",
    "source_billing_period",
    "source_minimum_commitment_months",
    "is_active",
]

EXTENDED_OFFER_SELECT = (
    ",".join(OFFER_MASTER_COLUMNS)
    + ",promo_offer_items(offer_item_id,offer_id,item_name,service_id,quantity:delivered_units,unit_price)"
    + ",clinic_promotions(promotion_id,business_id,source_url,promotion_title,campaign_start_date,campaign_end_date,is_active,promotion_content,is_offer_extracted,is_ocr_required)"
    + ",clinic_memberships(plan_id,business_id,source_url,membership_name,membership_price,billing_period,minimum_commitment_months,benefits)"
)


def parse_args() -> argparse.Namespace:
    today = datetime.now().strftime("%Y%m%d")
    default_dir = PROJECT_ROOT / ".firecrawl" / "clinic_memberships_plan_20260917"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(default_dir / f"promo_offer_master_qa_{today}.csv"),
        help="输出 CSV 路径",
    )
    parser.add_argument(
        "--summary",
        default=None,
        help="可选的 JSON 摘要路径；默认与 CSV 同目录同名",
    )
    return parser.parse_args()


def fetch_all(client: SupabaseRestClient) -> List[Dict[str, Any]]:
    return fetch_all_rows(client, "promo_offer_master", EXTENDED_OFFER_SELECT, page_size=PAGE_SIZE, order="id.asc")


def fetch_lookups(client: SupabaseRestClient) -> tuple[set, set, Dict[Any, Dict[str, Any]]]:
    master_rows = fetch_all_rows(client, "master_business_info", "business_id", order="business_id.asc")
    plan_rows = fetch_all_rows(client, "clinic_memberships", "plan_id", order="plan_id.asc")
    service_rows = fetch_all_rows(
        client,
        "clinic_services",
        "service_id,business_id,service_name,regular_price,unit_type,service_area,source_url,service_category",
        order="service_id.asc",
    )
    master_ids = {row["business_id"] for row in master_rows if row.get("business_id") is not None}
    plan_ids = {row["plan_id"] for row in plan_rows if row.get("plan_id") is not None}
    services = {
        row["service_id"]: row
        for row in service_rows
        if row.get("service_id") is not None
    }
    return master_ids, plan_ids, services


def embedded(row: Mapping[str, Any], key: str) -> Dict[str, Any]:
    value = row.get(key)
    if isinstance(value, list):
        return value[0] if value else {}
    return value if isinstance(value, dict) else {}


def items_for(row: Mapping[str, Any]) -> List[Dict[str, Any]]:
    value = row.get("promo_offer_items")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def decimal_cell(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return cell(value)
    return f"{number:.2f}"


def quantity_cell(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return cell(value)
    if number.is_integer():
        return str(int(number))
    return str(number)


def json_cell(value: Any) -> str:
    if value is None or value == [] or value == "":
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def newline_cells(values: Iterable[Any], formatter=cell) -> str:
    return "\n".join(formatter(value) for value in values)


def source_info(row: Mapping[str, Any]) -> tuple[str, Dict[str, Any]]:
    promo = embedded(row, "clinic_promotions")
    membership = embedded(row, "clinic_memberships")
    if row.get("promotion_id") is not None:
        return "promotion", promo
    if row.get("membership_plan_id") is not None:
        return "membership", membership
    return "", promo or membership


def minimum_quantity(conditions: Any) -> str:
    values = conditions if isinstance(conditions, list) else [conditions]
    for value in values:
        match = re.search(r"\bminimum\s+(\d+(?:\.\d+)?)\s+units?\b", str(value or ""), re.I)
        if match:
            return quantity_cell(match.group(1))
    return ""


def build_row(
    row: Mapping[str, Any],
    services: Mapping[Any, Mapping[str, Any]],
    issues_by_id: Mapping[Any, List[str]],
) -> Dict[str, Any]:
    source_type, source = source_info(row)
    item_rows = items_for(row)
    service_rows = [services.get(item.get("service_id"), {}) for item in item_rows]
    issue_notes = issues_by_id.get(row.get("id"), [])
    conditions = row.get("conditions")
    source_content = source.get("benefits") if source_type == "membership" else source.get("promotion_content")
    item_names = [service.get("service_name") or item.get("item_name") for item, service in zip(item_rows, service_rows)]

    output = {
        "qa_status": "FAIL" if issue_notes else "PASS",
        "qa_notes": "\n".join(issue_notes),
        "offer_id": cell(row.get("id")),
        "business_id": cell(row.get("business_id")),
        "promotion_id": cell(row.get("promotion_id")),
        "membership_plan_id": cell(row.get("membership_plan_id")),
        "source_type": source_type,
        "source_business_id": cell(source.get("business_id")),
        "source_title": cell(source.get("promotion_title") or source.get("membership_name")),
        "Check": "" if source_type != "promotion" or source.get("is_offer_extracted") is None else ("1" if source.get("is_offer_extracted") else "0"),
        "source_url": cell(source.get("source_url")),
        "item_names": newline_cells(item_names),
        "offer_raw_text": cell(row.get("offer_raw_text")),
        "regular_price": decimal_cell(row.get("regular_price")),
        "discount_price": decimal_cell(row.get("discount_price")),
        "discount_percent": decimal_cell(row.get("discount_percent")),
        "discount_amount": decimal_cell(row.get("discount_amount")),
        "conditions": json_cell(conditions),
        "service_categories": newline_cells(service.get("service_category") for service in service_rows),
        "delivered_quantity": newline_cells(item.get("quantity") for item in item_rows),
        "min_quantity": minimum_quantity(conditions),
        "discount unit_prices": newline_cells(
            (
                item.get("unit_price") if row.get("price_model") == "per_unit" else ""
                for item in item_rows
            ),
            formatter=decimal_cell,
        ),
        "service_regular_prices": newline_cells(
            (service.get("regular_price") for service in service_rows),
            formatter=decimal_cell,
        ),
        "is_membership_required": "true" if row.get("is_membership_required") else "false",
        "is_new_customer_required": "TRUE" if row.get("is_new_customer_required") else "FALSE",
        "source_content": json_cell(source_content),
        "service_unit_types": newline_cells(service.get("unit_type") for service in service_rows),
        "service_areas": newline_cells(service.get("service_area") for service in service_rows),
        "service_source_urls": newline_cells(service.get("source_url") for service in service_rows),
        "source_start_date": cell(source.get("campaign_start_date")),
        "source_end_date": cell(source.get("campaign_end_date")),
        "source_membership_price": decimal_cell(source.get("membership_price")),
        "source_billing_period": cell(source.get("billing_period")),
        "source_minimum_commitment_months": cell(source.get("minimum_commitment_months")),
        "is_active": "TRUE" if row.get("is_active") else "FALSE",
    }
    return {key: output.get(key, "") for key in QA_COLUMNS}


def main() -> None:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    summary_path = (
        Path(args.summary).expanduser().resolve()
        if args.summary
        else output_path.with_name(output_path.stem + "_summary.json")
    )

    client = load_supabase_client(PROJECT_ROOT, role="writer")
    rows = fetch_all(client)
    master_ids, plan_ids, services = fetch_lookups(client)
    issues, _, layered_summary = audit_rows(
        rows,
        master_business_ids=master_ids,
        membership_plan_ids=plan_ids,
        service_rows=services,
    )
    issues_by_id: Dict[Any, List[str]] = defaultdict(list)
    issue_types = Counter()
    for issue in issues:
        issues_by_id[issue.id].append(f"{issue.issue_type} | {issue.detail}")
        issue_types[issue.issue_type] += 1

    export_rows = [build_row(row, services, issues_by_id) for row in rows]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=QA_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(export_rows)

    status_counts = Counter(row["qa_status"] for row in export_rows)
    summary = {
        "table": "promo_offer_master_qa",
        "source_table": "promo_offer_master",
        "exported_at": datetime.now().isoformat(),
        "row_count": len(export_rows),
        "qa_status_counts": dict(status_counts),
        "audit_issue_count": len(issues),
        "audit_issue_type_counts": dict(issue_types),
        "active_rows": sum(row.get("is_active") == "TRUE" for row in export_rows),
        "audit_summary": layered_summary,
        "qa_scope": "当前 live 数据的结构性/关系完整性审计；本次未重新逐条打开来源页面，因此不将 source-page 语义复核表述为已完成。",
        "columns": QA_COLUMNS,
        "csv_path": str(output_path),
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"csv_path": str(output_path), "summary_path": str(summary_path), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
