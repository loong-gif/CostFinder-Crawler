#!/usr/bin/env python3
"""
Apply the generated safe remediation plan to promo_offer_master via Supabase REST.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING  # noqa: E402


TABLE_NAME = "promo_offer_master"
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"


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

    def fetch_rows_by_ids(self, table: str, ids: Sequence[int]) -> List[Dict[str, Any]]:
        if not ids:
            return []
        id_list = ",".join(str(int(item)) for item in sorted(set(ids)))
        response = self.session.get(
            f"{self.base_url}/{table}",
            params={"select": "*", "id": f"in.({id_list})", "order": "id.asc"},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def update_row(self, table: str, row_id: int, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = self.session.patch(
            f"{self.base_url}/{table}",
            params={"id": f"eq.{row_id}"},
            headers={"Prefer": "return=representation"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def delete_rows(self, table: str, ids: Sequence[int]) -> Optional[str]:
        if not ids:
            return None
        id_list = ",".join(str(int(item)) for item in sorted(set(ids)))
        response = self.session.delete(
            f"{self.base_url}/{table}",
            params={"id": f"in.({id_list})"},
            headers={"Prefer": "return=representation"},
            timeout=60,
        )
        response.raise_for_status()
        return response.headers.get("Content-Range")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply promo_offer_master safe fix plan")
    parser.add_argument("--plan", required=True, help="Path to promo_offer_master_safe_fix_plan CSV")
    parser.add_argument("--dry-run", action="store_true", help="Only create backup and execution preview")
    return parser.parse_args()


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def read_plan(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding=OUTPUT_ENCODING, newline="") as handle:
        return list(csv.DictReader(handle))


def parse_id_list(raw: str) -> List[int]:
    text = (raw or "").strip()
    if not text:
        return []
    payload = json.loads(text)
    if not isinstance(payload, list):
        return []
    values: List[int] = []
    for item in payload:
        try:
            values.append(int(item))
        except (TypeError, ValueError):
            continue
    return values


def parse_payload(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    payload = json.loads(text)
    if not isinstance(payload, dict):
        return {}
    return payload


def collect_target_ids(plan_rows: Sequence[Dict[str, str]]) -> List[int]:
    ids: List[int] = []
    for row in plan_rows:
        action = row.get("action", "")
        if action == "delete_exact_duplicate":
            keep_id = row.get("keep_id", "").strip()
            if keep_id:
                ids.append(int(keep_id))
            ids.extend(parse_id_list(row.get("delete_ids", "")))
        else:
            row_id = row.get("id", "").strip()
            if row_id:
                ids.append(int(row_id))
    return sorted(set(ids))


def build_backup_path(plan_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"promo_offer_master_safe_fix_backup_{timestamp}.json"


def build_summary_path(plan_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"promo_offer_master_safe_fix_execution_{timestamp}.json"


def main() -> None:
    args = parse_args()
    plan_path = Path(args.plan).expanduser().resolve()
    plan_rows = read_plan(plan_path)
    client = load_client()

    target_ids = collect_target_ids(plan_rows)
    backup_rows = client.fetch_rows_by_ids(TABLE_NAME, target_ids)

    backup_path = build_backup_path(plan_path)
    backup_path.write_text(json.dumps(backup_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    execution_log: List[Dict[str, Any]] = []
    counts = {
        "delete_exact_duplicate": 0,
        "update_service_name": 0,
        "update_unit_type": 0,
        "update_fields": 0,
        "errors": 0,
    }

    if not args.dry_run:
        for row in plan_rows:
            action = row.get("action", "")
            try:
                if action == "delete_exact_duplicate":
                    delete_ids = parse_id_list(row.get("delete_ids", ""))
                    if delete_ids:
                        client.delete_rows(TABLE_NAME, delete_ids)
                        counts[action] += 1
                        execution_log.append({"action": action, "delete_ids": delete_ids, "status": "applied"})
                elif action == "update_service_name":
                    row_id = int(row["id"])
                    payload = {"service_name": row["to_service_name"]}
                    client.update_row(TABLE_NAME, row_id, payload)
                    counts[action] += 1
                    execution_log.append({"action": action, "id": row_id, "payload": payload, "status": "applied"})
                elif action == "update_unit_type":
                    row_id = int(row["id"])
                    payload = {"unit_type": row["target_unit_type"]}
                    client.update_row(TABLE_NAME, row_id, payload)
                    counts[action] += 1
                    execution_log.append({"action": action, "id": row_id, "payload": payload, "status": "applied"})
                elif action == "update_fields":
                    row_id = int(row["id"])
                    payload = parse_payload(row.get("payload_json", ""))
                    if payload:
                        client.update_row(TABLE_NAME, row_id, payload)
                        counts[action] += 1
                        execution_log.append({"action": action, "id": row_id, "payload": payload, "status": "applied"})
            except Exception as exc:  # pragma: no cover
                counts["errors"] += 1
                execution_log.append({"action": action, "row": row, "status": "error", "error": str(exc)})

    remaining_rows = client.fetch_rows_by_ids(TABLE_NAME, target_ids)
    remaining_by_id = {row["id"]: row for row in remaining_rows if "id" in row}

    verification = {
        "deleted_ids_missing": [],
        "service_name_mismatches": [],
        "unit_type_mismatches": [],
        "field_update_mismatches": [],
    }
    for row in plan_rows:
        action = row.get("action", "")
        if action == "delete_exact_duplicate":
            for delete_id in parse_id_list(row.get("delete_ids", "")):
                if delete_id not in remaining_by_id:
                    verification["deleted_ids_missing"].append(delete_id)
        elif action == "update_service_name":
            row_id = int(row["id"])
            actual = (remaining_by_id.get(row_id) or {}).get("service_name")
            if actual != row["to_service_name"]:
                verification["service_name_mismatches"].append({"id": row_id, "expected": row["to_service_name"], "actual": actual})
        elif action == "update_unit_type":
            row_id = int(row["id"])
            actual = (remaining_by_id.get(row_id) or {}).get("unit_type")
            if actual != row["target_unit_type"]:
                verification["unit_type_mismatches"].append({"id": row_id, "expected": row["target_unit_type"], "actual": actual})
        elif action == "update_fields":
            row_id = int(row["id"])
            current = remaining_by_id.get(row_id) or {}
            payload = parse_payload(row.get("payload_json", ""))
            mismatches = {}
            for key, expected in payload.items():
                actual = current.get(key)
                if str(actual) != str(expected):
                    mismatches[key] = {"expected": expected, "actual": actual}
            if mismatches:
                verification["field_update_mismatches"].append({"id": row_id, "mismatches": mismatches})

    summary = {
        "plan_path": str(plan_path),
        "backup_path": str(backup_path),
        "dry_run": bool(args.dry_run),
        "counts": counts,
        "target_row_count": len(target_ids),
        "verification": {
            "deleted_ids_missing_count": len(verification["deleted_ids_missing"]),
            "service_name_mismatch_count": len(verification["service_name_mismatches"]),
            "unit_type_mismatch_count": len(verification["unit_type_mismatches"]),
            "field_update_mismatch_count": len(verification["field_update_mismatches"]),
        },
        "verification_details": verification,
        "execution_log_sample": execution_log[:50],
    }

    summary_path = build_summary_path(plan_path)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary_path": str(summary_path), "backup_path": str(backup_path), **summary["counts"], **summary["verification"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
