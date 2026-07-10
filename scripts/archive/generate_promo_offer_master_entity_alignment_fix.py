#!/usr/bin/env python3
"""
Generate a conservative, data-only remediation plan for promo_offer_master entity alignment issues.
Only high-confidence single-entity cases are included.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
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
TARGET_ISSUES = {"service_name_manual_review", "service_offer_content_entity_mismatch"}
ALLOWED_TEMPLATE_TYPES = {"FIXED_PRICE", "DISCOUNT", "COMPLIMENTARY"}
SKIP_CANONICALS = {"Package"}


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate entity alignment fix plan for promo_offer_master")
    parser.add_argument("--timestamp", default=None, help="指定 audit 时间戳，例如 20260418_154241；默认使用最新一批")
    return parser.parse_args()


def latest_file(pattern: str) -> Path:
    matches = sorted(OUTPUT_DIR.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"未找到匹配文件: {pattern}")
    return matches[-1]


def resolve_inputs(timestamp: Optional[str]) -> Dict[str, Path]:
    suffix = f"_{timestamp}" if timestamp else "_*"
    return {
        "issues": latest_file(f"promo_offer_master_audit_issues{suffix}.csv"),
        "alignment": latest_file(f"promo_offer_master_service_alignment{suffix}.csv"),
        "summary": latest_file(f"promo_offer_master_audit_summary{suffix}.json"),
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
    issue_rows = read_csv(inputs["issues"])
    alignment_rows = {row["id"]: row for row in read_csv(inputs["alignment"])}
    client = load_client()

    issue_ids = sorted({int(row["id"]) for row in issue_rows if row.get("issue_type") in TARGET_ISSUES})
    current_rows = client.fetch_rows_by_ids(
        "promo_offer_master",
        issue_ids,
        select="id,source_name,service_name,offer_content,template_type",
    )

    generated_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []

    for row in current_rows:
        row_id = str(row["id"])
        alignment = alignment_rows.get(row_id)
        if not alignment:
            continue
        canonical = (alignment.get("aligned_service_name_canonical") or "").strip()
        template_type = str(row.get("template_type") or "").strip().upper()
        offer_content = row.get("offer_content")
        issue_types = sorted({item["issue_type"] for item in issue_rows if item["id"] == row_id and item["issue_type"] in TARGET_ISSUES})

        if not canonical:
            review_rows.append({"id": row_id, "source_name": row.get("source_name", ""), "service_name": row.get("service_name", ""), "reason": "canonical 为空"})
            continue
        if template_type == "BUNDLE" and ";" in canonical:
            if str(row.get("service_name") or "").strip() != "Package":
                plan_rows.append(
                    {
                        "action": "update_fields",
                        "id": row_id,
                        "payload_json": json.dumps({"service_name": "Package"}, ensure_ascii=False, sort_keys=True),
                        "issue_types": ";".join(issue_types),
                        "source_name": row.get("source_name", ""),
                        "service_name": row.get("service_name", ""),
                        "note": "service_name -> Package for multi-entity bundle",
                    }
                )
            else:
                review_rows.append({"id": row_id, "source_name": row.get("source_name", ""), "service_name": row.get("service_name", ""), "reason": "multi-entity bundle 已使用 Package"})
            continue
        if ";" in canonical or canonical in SKIP_CANONICALS:
            review_rows.append({"id": row_id, "source_name": row.get("source_name", ""), "service_name": row.get("service_name", ""), "reason": f"canonical 不适合自动修复: {canonical}"})
            continue
        if template_type not in ALLOWED_TEMPLATE_TYPES:
            review_rows.append({"id": row_id, "source_name": row.get("source_name", ""), "service_name": row.get("service_name", ""), "reason": f"template_type={template_type} 不在安全自动修复范围"})
            continue
        if not isinstance(offer_content, dict) or len(offer_content) != 1:
            review_rows.append({"id": row_id, "source_name": row.get("source_name", ""), "service_name": row.get("service_name", ""), "reason": "offer_content 不是单实体字典"})
            continue

        key, value = next(iter(offer_content.items()))
        payload: Dict[str, Any] = {}
        notes: List[str] = []
        if str(row.get("service_name") or "").strip() != canonical:
            payload["service_name"] = canonical
            notes.append(f"service_name -> {canonical}")
        if key != canonical:
            payload["offer_content"] = {canonical: value}
            notes.append(f"offer_content -> {{{canonical}: {value}}}")
        if not payload:
            review_rows.append({"id": row_id, "source_name": row.get("source_name", ""), "service_name": row.get("service_name", ""), "reason": "当前值已与 canonical 对齐"})
            continue

        plan_rows.append(
            {
                "action": "update_fields",
                "id": row_id,
                "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "issue_types": ";".join(issue_types),
                "source_name": row.get("source_name", ""),
                "service_name": row.get("service_name", ""),
                "note": " | ".join(notes),
            }
        )

    summary = {
        "generated_at": generated_at,
        "source_summary_path": str(inputs["summary"]),
        "planned_row_count": len(plan_rows),
        "review_row_count": len(review_rows),
        "planned_action_counts": dict(Counter(row["action"] for row in plan_rows)),
    }

    plan_path = OUTPUT_DIR / f"promo_offer_master_entity_alignment_fix_plan_{generated_at}.csv"
    review_path = OUTPUT_DIR / f"promo_offer_master_entity_alignment_fix_review_{generated_at}.csv"
    summary_path = OUTPUT_DIR / f"promo_offer_master_entity_alignment_fix_summary_{generated_at}.json"

    write_csv(plan_path, plan_rows)
    write_csv(review_path, review_rows)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "plan_path": str(plan_path),
                "review_path": str(review_path),
                "summary_path": str(summary_path),
                "planned_row_count": len(plan_rows),
                "review_row_count": len(review_rows),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
