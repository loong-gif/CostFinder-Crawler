#!/usr/bin/env python3
"""
Generate a second-stage, data-only remediation plan for promo_offer_master.
This phase only applies high-confidence value fixes and does not change schema.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING  # noqa: E402


OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
TABLE_NAME = "promo_offer_master"

SAFE_ISSUE_TYPES = {
    "membership_missing_name",
    "discount_missing_discount_fields",
    "unit_type_missing_unit",
    "unit_type_unit_missing_quantity",
    "offer_content_empty_or_unstructured",
}

MANUAL_REVIEW_ISSUE_TYPES = {
    "service_name_manual_review",
    "service_offer_content_entity_mismatch",
    "membership_missing_name",
    "discount_missing_discount_fields",
    "offer_content_empty_or_unstructured",
    "discount_price_gt_original_price",
    "membership_price_zero",
    "unit_type_unit_missing_quantity",
}


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows_by_ids(self, table: str, ids: List[int], select: str = "*") -> List[Dict[str, Any]]:
        if not ids:
            return []
        rows: List[Dict[str, Any]] = []
        unique_ids = sorted(set(int(item) for item in ids))
        for start in range(0, len(unique_ids), 100):
            chunk = unique_ids[start : start + 100]
            response = self.session.get(
                f"{self.base_url}/{table}",
                params={"select": select, "id": f"in.({','.join(str(item) for item in chunk)})", "order": "id.asc"},
                timeout=60,
            )
            response.raise_for_status()
            rows.extend(response.json())
        return rows

    def fetch_master_rows_by_business_ids(self, ids: List[int], select: str = "business_id,name,membership") -> List[Dict[str, Any]]:
        if not ids:
            return []
        rows: List[Dict[str, Any]] = []
        unique_ids = sorted(set(int(item) for item in ids))
        for start in range(0, len(unique_ids), 100):
            chunk = unique_ids[start : start + 100]
            response = self.session.get(
                f"{self.base_url}/master_business_info",
                params={"select": select, "business_id": f"in.({','.join(str(item) for item in chunk)})", "order": "business_id.asc"},
                timeout=60,
            )
            response.raise_for_status()
            rows.extend(response.json())
        return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate promo_offer_master second-stage safe fix plan")
    parser.add_argument("--timestamp", default=None, help="指定 audit 时间戳，例如 20260418_151118；默认使用最新一批")
    return parser.parse_args()


def latest_file(pattern: str) -> Path:
    matches = sorted(OUTPUT_DIR.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"未找到匹配文件: {pattern}")
    return matches[-1]


def resolve_inputs(timestamp: Optional[str]) -> Dict[str, Path]:
    suffix = f"_{timestamp}" if timestamp else "_*"
    return {
        "summary": latest_file(f"promo_offer_master_audit_summary{suffix}.json"),
        "issues": latest_file(f"promo_offer_master_audit_issues{suffix}.csv"),
        "exact_duplicates": latest_file(f"promo_offer_master_exact_duplicates{suffix}.csv"),
    }


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding=OUTPUT_ENCODING, newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_ids(raw: str) -> List[int]:
    text = (raw or "").strip()
    if not text:
        return []
    payload = json.loads(text)
    if not isinstance(payload, list):
        return []
    return [int(item) for item in payload]


def discount_value_from_text(text: str) -> Dict[str, Any]:
    percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*off\b", text, flags=re.IGNORECASE)
    if percent_match:
        return {"discount_percent": float(percent_match.group(1))}

    save_amount_match = re.search(r"\bsave\s+\$(\d+(?:\.\d+)?)\b", text, flags=re.IGNORECASE)
    if save_amount_match:
        amount = float(save_amount_match.group(1))
        if amount.is_integer():
            amount = int(amount)
        return {"discount_amount": amount}

    amount_match = re.search(r"\$(\d+(?:\.\d+)?)\s*off\b", text, flags=re.IGNORECASE)
    if amount_match:
        amount = float(amount_match.group(1))
        if amount.is_integer():
            amount = int(amount)
        return {"discount_amount": amount}

    return {}


def title_case_phrase(value: str) -> str:
    words = re.split(r"(\s+)", value.strip())
    return "".join(word.capitalize() if word.strip() else word for word in words)


def infer_membership_name(row: Dict[str, Any]) -> Optional[str]:
    service_name = str(row.get("service_name") or "").strip()
    text = str(row.get("offer_raw_text") or "").strip()
    normalized_text = re.sub(r"\s+", " ", text)

    if service_name.lower().endswith("membership"):
        return service_name

    m = re.search(r"^([A-Za-z][A-Za-z '&]+?)\s*[–-]\s*\$\d+(?:\.\d+)?\s*/\s*month", normalized_text, flags=re.IGNORECASE)
    if m:
        return title_case_phrase(m.group(1))

    m = re.search(r"\b(VIP Membership)\b", normalized_text, flags=re.IGNORECASE)
    if m:
        return "VIP Membership"

    m = re.search(r"\b(AESTHETIC\s+Pure)\b", normalized_text, flags=re.IGNORECASE)
    if m:
        return "Aesthetic Pure Membership"

    return None


def parse_membership_value(raw_value: Any) -> List[Dict[str, Any]]:
    if raw_value in (None, "", "null"):
        return []
    if isinstance(raw_value, list):
        parsed = raw_value
    else:
        text = str(raw_value).strip()
        if not text or text.lower() == '"not provided"':
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def parse_price(value: Any) -> Optional[float]:
    if value in (None, "", "null", "not provided"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def infer_membership_name_from_master(row: Dict[str, Any], master_row: Optional[Dict[str, Any]]) -> Optional[str]:
    memberships = parse_membership_value((master_row or {}).get("membership"))
    if not memberships:
        return None

    service_name = str(row.get("service_name") or "").strip().lower()
    offer_text = str(row.get("offer_raw_text") or "").strip().lower()
    promo_membership_price = parse_price(row.get("membership_price"))

    exact_name_matches: List[Dict[str, Any]] = []
    for membership in memberships:
        membership_name = str(membership.get("membership_name") or "").strip()
        if membership_name and membership_name.lower() in offer_text:
            exact_name_matches.append(membership)

    service_matched = [
        membership
        for membership in exact_name_matches
        if service_name and service_name in str(membership.get("membership_name") or "").strip().lower()
    ]
    if len(service_matched) == 1:
        return str(service_matched[0].get("membership_name") or "").strip() or None

    if len(exact_name_matches) == 1:
        if "depending on membership" in offer_text or "membership benefit" in offer_text:
            return None
        return str(exact_name_matches[0].get("membership_name") or "").strip() or None

    if len(memberships) == 1:
        return str(memberships[0].get("membership_name") or "").strip() or None

    if promo_membership_price is not None:
        price_matches = []
        for membership in memberships:
            master_price = parse_price(membership.get("membership_price"))
            if master_price is not None and abs(master_price - promo_membership_price) < 0.01:
                price_matches.append(membership)
        if len(price_matches) == 1:
            return str(price_matches[0].get("membership_name") or "").strip() or None

    return None


def infer_offer_content(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    source_name = str(row.get("source_name") or "").strip()
    service_name = str(row.get("service_name") or "").strip()
    template_type = str(row.get("template_type") or "").strip().upper()
    membership_name = str(row.get("membership_name") or "").strip()

    if source_name == "New Look Skin Center" and service_name == "PDO Threads":
        return {"PDO Threads": 1}

    if source_name == "Ageless MD" and membership_name == "VIP Membership" and service_name in {"Neurotoxin", "Dermal Filler"}:
        return {service_name: 1}

    if template_type == "FIXED_PRICE" and service_name in {"PDO Threads"}:
        return {service_name: 1}

    return None


def merge_payload(plan_by_id: Dict[int, Dict[str, Any]], row_id: int, payload: Dict[str, Any], issue_type: str, note: str, row: Dict[str, Any]) -> None:
    if not payload:
        return
    entry = plan_by_id.setdefault(
        row_id,
        {
            "action": "update_fields",
            "id": row_id,
            "payload": {},
            "issue_types": set(),
            "source_name": row.get("source_name", ""),
            "service_name": row.get("service_name", ""),
            "notes": [],
        },
    )
    entry["payload"].update(payload)
    entry["issue_types"].add(issue_type)
    entry["notes"].append(note)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding=OUTPUT_ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    inputs = resolve_inputs(args.timestamp)
    summary = read_json(inputs["summary"])
    issue_rows = read_csv(inputs["issues"])
    duplicate_rows = read_csv(inputs["exact_duplicates"])
    client = load_client()

    issue_ids = [int(row["id"]) for row in issue_rows if row.get("issue_type") in SAFE_ISSUE_TYPES]
    fetched_rows = client.fetch_rows_by_ids(
        TABLE_NAME,
        issue_ids,
        select="id,business_id,source_name,service_name,template_type,offer_raw_text,membership_name,membership_price,discount_price,original_price,discount_amount,discount_percent,unit_type,min_unit,delivered_unit,is_membership_required",
    )
    rows_by_id = {int(row["id"]): row for row in fetched_rows}
    master_rows = client.fetch_master_rows_by_business_ids(
        [int(row["business_id"]) for row in fetched_rows if row.get("business_id") is not None]
    )
    master_by_business_id = {int(row["business_id"]): row for row in master_rows if row.get("business_id") is not None}

    generated_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_rows: List[Dict[str, Any]] = []
    plan_by_id: Dict[int, Dict[str, Any]] = {}
    review_rows: List[Dict[str, Any]] = []

    for row in duplicate_rows:
        ids = parse_ids(row.get("ids", ""))
        if len(ids) < 2:
            continue
        keep_id = min(ids)
        delete_ids = sorted(item for item in ids if item != keep_id)
        if not delete_ids:
            continue
        plan_rows.append(
            {
                "action": "delete_exact_duplicate",
                "keep_id": keep_id,
                "delete_ids": json.dumps(delete_ids, ensure_ascii=False),
                "source_name": row.get("source_name", ""),
                "service_name": row.get("service_name", ""),
                "offer_raw_text": row.get("offer_raw_text", ""),
            }
        )

    for issue in issue_rows:
        issue_type = issue.get("issue_type", "")
        row_id = int(issue["id"])
        current = rows_by_id.get(row_id)
        if not current:
            continue

        if issue_type == "membership_missing_name":
            if current.get("membership_name"):
                continue
            business_id = current.get("business_id")
            master_row = master_by_business_id.get(int(business_id)) if business_id is not None else None
            inferred_name = infer_membership_name_from_master(current, master_row) or infer_membership_name(current)
            if inferred_name:
                merge_payload(plan_by_id, row_id, {"membership_name": inferred_name}, issue_type, f"补齐 membership_name={inferred_name}", current)
            else:
                review_rows.append({**issue, "review_reason": "membership_name 无法安全推断"})

        elif issue_type == "discount_missing_discount_fields":
            payload = discount_value_from_text(str(current.get("offer_raw_text") or ""))
            if payload:
                safe_payload = {key: value for key, value in payload.items() if current.get(key) in (None, "", "0", 0)}
                if safe_payload:
                    merge_payload(plan_by_id, row_id, safe_payload, issue_type, f"从 offer_raw_text 解析 {safe_payload}", current)
                else:
                    review_rows.append({**issue, "review_reason": "discount 字段已有值或无需覆盖"})
            else:
                review_rows.append({**issue, "review_reason": "discount 信息无法从文本高置信解析"})

        elif issue_type == "unit_type_missing_unit":
            if str(current.get("unit_type") or "").strip().lower() != "unit":
                merge_payload(plan_by_id, row_id, {"unit_type": "unit"}, issue_type, "修正 unit_type 为 unit", current)

        elif issue_type == "unit_type_unit_missing_quantity":
            payload: Dict[str, Any] = {}
            if current.get("min_unit") in (None, "", "null"):
                payload["min_unit"] = 1
            if current.get("delivered_unit") in (None, "", "null"):
                payload["delivered_unit"] = 1
            if payload:
                merge_payload(plan_by_id, row_id, payload, issue_type, "补齐 unit 默认数量为 1", current)
            else:
                review_rows.append({**issue, "review_reason": "unit 数量字段已存在，无需更新"})

        elif issue_type == "offer_content_empty_or_unstructured":
            inferred_offer_content = infer_offer_content(current)
            if inferred_offer_content:
                merge_payload(plan_by_id, row_id, {"offer_content": inferred_offer_content}, issue_type, f"补齐 offer_content={inferred_offer_content}", current)
            else:
                review_rows.append({**issue, "review_reason": "offer_content 无法安全推断"})

        elif issue_type in MANUAL_REVIEW_ISSUE_TYPES:
            review_rows.append({**issue, "review_reason": "需要人工复核"})

    plan_rows.extend(
        {
            "action": value["action"],
            "id": value["id"],
            "payload_json": json.dumps(value["payload"], ensure_ascii=False, sort_keys=True),
            "issue_types": ";".join(sorted(value["issue_types"])),
            "source_name": value["source_name"],
            "service_name": value["service_name"],
            "note": " | ".join(value["notes"]),
        }
        for _, value in sorted(plan_by_id.items())
    )

    planned_ids = {int(row["id"]) for row in plan_rows if row.get("id")}
    for issue in issue_rows:
        if issue.get("issue_type") in MANUAL_REVIEW_ISSUE_TYPES and int(issue["id"]) not in planned_ids:
            review_rows.append({**issue, "review_reason": "当前未纳入自动修复"})

    deduped_review_rows: List[Dict[str, Any]] = []
    seen_review_keys = set()
    for row in review_rows:
        key = (row.get("id"), row.get("issue_type"), row.get("review_reason"))
        if key in seen_review_keys:
            continue
        seen_review_keys.add(key)
        deduped_review_rows.append(row)

    plan_counts = Counter(row["action"] for row in plan_rows)
    summary_payload = {
        "generated_at": generated_at,
        "audit_summary_path": str(inputs["summary"]),
        "audit_row_count": summary.get("row_count"),
        "audit_total_issues": summary.get("total_issues"),
        "planned_action_counts": dict(plan_counts),
        "planned_row_count": len(plan_rows),
        "review_row_count": len(deduped_review_rows),
    }

    plan_path = OUTPUT_DIR / f"promo_offer_master_safe_fix_phase2_plan_{generated_at}.csv"
    review_path = OUTPUT_DIR / f"promo_offer_master_safe_fix_phase2_review_{generated_at}.csv"
    summary_path = OUTPUT_DIR / f"promo_offer_master_safe_fix_phase2_summary_{generated_at}.json"

    write_csv(plan_path, plan_rows)
    write_csv(review_path, deduped_review_rows)
    summary_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "plan_path": str(plan_path),
                "review_path": str(review_path),
                "summary_path": str(summary_path),
                "planned_row_count": len(plan_rows),
                "review_row_count": len(deduped_review_rows),
                "planned_action_counts": dict(plan_counts),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
