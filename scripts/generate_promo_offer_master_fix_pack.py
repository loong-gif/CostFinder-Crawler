#!/usr/bin/env python3
"""
Generate a safe SQL remediation pack for promo_offer_master based on the latest audit outputs.
"""
from __future__ import annotations

import argparse
import ast
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
OUTPUT_ENCODING = "utf-8-sig"

SAFE_REVIEW_ISSUES = {
    "service_name_manual_review",
    "service_offer_content_entity_mismatch",
    "discount_missing_discount_fields",
    "discount_price_gt_original_price",
    "membership_missing_name",
    "offer_content_empty_or_unstructured",
    "membership_price_zero",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate SQL fix pack for promo_offer_master audit results")
    parser.add_argument("--timestamp", default=None, help="指定 audit 时间戳，例如 20260418_144506；默认使用最新一批")
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
        "text_duplicates": latest_file(f"promo_offer_master_offer_text_duplicates{suffix}.csv"),
        "alignment": latest_file(f"promo_offer_master_service_alignment{suffix}.csv"),
    }


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding=OUTPUT_ENCODING, newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_ids(raw: str) -> List[int]:
    text = (raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = ast.literal_eval(text)
    if not isinstance(parsed, list):
        return []
    ids: List[int] = []
    for item in parsed:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return ids


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_duplicate_delete_statements(rows: Iterable[Dict[str, str]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    statements: List[str] = []
    plan_rows: List[Dict[str, Any]] = []
    for row in rows:
        ids = parse_ids(row.get("ids", ""))
        if len(ids) < 2:
            continue
        keep_id = min(ids)
        delete_ids = sorted(id_value for id_value in ids if id_value != keep_id)
        if not delete_ids:
            continue
        delete_list = ", ".join(str(id_value) for id_value in delete_ids)
        statements.append(
            f"-- Duplicate cleanup: keep id={keep_id}, delete duplicates for {row.get('source_name','')} / {row.get('service_name','')}\n"
            f"DELETE FROM promo_offer_master WHERE id IN ({delete_list});"
        )
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
    return statements, plan_rows


def build_service_name_update_statements(rows: Iterable[Dict[str, str]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    statements: List[str] = []
    plan_rows: List[Dict[str, Any]] = []
    for row in rows:
        canonical = (row.get("aligned_service_name_canonical") or "").strip()
        service_name = (row.get("service_name") or "").strip()
        needs_manual_review = (row.get("needs_manual_review") or "").strip().upper() == "TRUE"
        if not canonical or canonical == service_name or needs_manual_review:
            continue
        if ";" in canonical:
            continue
        try:
            row_id = int(row.get("id") or "")
        except ValueError:
            continue
        statements.append(
            f"-- Canonicalize service_name for id={row_id}\n"
            f"UPDATE promo_offer_master SET service_name = {sql_quote(canonical)} WHERE id = {row_id};"
        )
        plan_rows.append(
            {
                "action": "update_service_name",
                "id": row_id,
                "from_service_name": service_name,
                "to_service_name": canonical,
                "source_name": row.get("source_name", ""),
                "alignment_note": row.get("alignment_note", ""),
            }
        )
    return statements, plan_rows


def build_unit_type_statements(issue_rows: Iterable[Dict[str, str]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    statements: List[str] = []
    plan_rows: List[Dict[str, Any]] = []
    seen_ids = set()
    for row in issue_rows:
        issue_type = row.get("issue_type", "")
        target_unit_type = ""
        if issue_type == "unit_type_missing_unit":
            target_unit_type = "unit"
        elif issue_type == "unit_type_missing_syringe":
            target_unit_type = "syringe"
        if not target_unit_type:
            continue
        try:
            row_id = int(row.get("id") or "")
        except ValueError:
            continue
        if row_id in seen_ids:
            continue
        seen_ids.add(row_id)
        statements.append(
            f"-- Normalize unit_type for id={row_id}\n"
            f"UPDATE promo_offer_master SET unit_type = {sql_quote(target_unit_type)} WHERE id = {row_id};"
        )
        plan_rows.append(
            {
                "action": "update_unit_type",
                "id": row_id,
                "target_unit_type": target_unit_type,
                "source_name": row.get("source_name", ""),
                "service_name": row.get("service_name", ""),
                "detail": row.get("detail", ""),
            }
        )
    return statements, plan_rows


def build_risky_review_rows(issue_rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    return [row for row in issue_rows if row.get("issue_type") in SAFE_REVIEW_ISSUES]


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


def build_sql(
    *,
    summary: Dict[str, Any],
    duplicate_statements: List[str],
    service_name_statements: List[str],
    unit_type_statements: List[str],
) -> str:
    counts = {
        "duplicate_delete_statements": len(duplicate_statements),
        "service_name_update_statements": len(service_name_statements),
        "unit_type_update_statements": len(unit_type_statements),
    }
    return f"""-- promo_offer_master safe remediation pack
-- generated_at: {datetime.now().isoformat()}
-- audited_rows: {summary.get('row_count')}
-- exact_duplicate_groups: {summary.get('exact_duplicate_groups')}
-- total_issues: {summary.get('total_issues')}
-- planned_actions: {json.dumps(counts, ensure_ascii=False)}

BEGIN;

-- 1) Safe exact-duplicate deletes
{chr(10).join(duplicate_statements) if duplicate_statements else '-- none'}

-- 2) Canonical service_name updates (single canonical entity only)
{chr(10).join(service_name_statements) if service_name_statements else '-- none'}

-- 3) unit_type normalization for clear by-unit / by-syringe cases
{chr(10).join(unit_type_statements) if unit_type_statements else '-- none'}

COMMIT;
"""


def main() -> None:
    args = parse_args()
    inputs = resolve_inputs(args.timestamp)
    summary = read_json(inputs["summary"])
    issue_rows = read_csv(inputs["issues"])
    exact_duplicate_rows = read_csv(inputs["exact_duplicates"])
    alignment_rows = read_csv(inputs["alignment"])

    duplicate_statements, duplicate_plan_rows = build_duplicate_delete_statements(exact_duplicate_rows)
    service_name_statements, service_name_plan_rows = build_service_name_update_statements(alignment_rows)
    unit_type_statements, unit_type_plan_rows = build_unit_type_statements(issue_rows)
    risky_review_rows = build_risky_review_rows(issue_rows)

    generated_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    sql_path = OUTPUT_DIR / f"promo_offer_master_safe_fix_{generated_at}.sql"
    plan_path = OUTPUT_DIR / f"promo_offer_master_safe_fix_plan_{generated_at}.csv"
    risky_review_path = OUTPUT_DIR / f"promo_offer_master_risky_review_{generated_at}.csv"
    summary_path = OUTPUT_DIR / f"promo_offer_master_fix_pack_summary_{generated_at}.json"

    sql_path.write_text(
        build_sql(
            summary=summary,
            duplicate_statements=duplicate_statements,
            service_name_statements=service_name_statements,
            unit_type_statements=unit_type_statements,
        ),
        encoding="utf-8",
    )

    plan_rows = duplicate_plan_rows + service_name_plan_rows + unit_type_plan_rows
    write_csv(plan_path, plan_rows)
    write_csv(risky_review_path, risky_review_rows)

    payload = {
        "audit_summary_path": str(inputs["summary"]),
        "audit_issues_path": str(inputs["issues"]),
        "safe_sql_path": str(sql_path),
        "safe_plan_path": str(plan_path),
        "risky_review_path": str(risky_review_path),
        "counts": {
            "duplicate_deletes": len(duplicate_plan_rows),
            "service_name_updates": len(service_name_plan_rows),
            "unit_type_updates": len(unit_type_plan_rows),
            "risky_review_rows": len(risky_review_rows),
        },
        "issue_type_counts_in_risky_review": dict(Counter(row["issue_type"] for row in risky_review_rows)),
    }
    summary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
