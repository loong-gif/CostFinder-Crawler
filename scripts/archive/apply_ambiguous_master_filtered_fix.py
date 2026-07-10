#!/usr/bin/env python3
"""
Resolve master_business_info domains that have both filtered and non-filtered rows.

For each ambiguous normalized domain, this script targets the non-filtered master
rows only. It first tries to delete each targeted master row. If deletion fails,
usually because of a foreign-key dependency, it falls back to setting
process_flag='filtered'.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
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


OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 1000


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

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"select": select}
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        if order:
            params["order"] = order
        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def fetch_rows_by_ids(self, table: str, id_column: str, ids: Sequence[int], select: str = "*") -> List[Dict[str, Any]]:
        if not ids:
            return []
        rows: List[Dict[str, Any]] = []
        unique_ids = sorted(set(int(item) for item in ids))
        for start in range(0, len(unique_ids), 100):
            chunk = unique_ids[start : start + 100]
            response = self.session.get(
                f"{self.base_url}/{table}",
                params={"select": select, id_column: f"in.({','.join(str(item) for item in chunk)})", "order": f"{id_column}.asc"},
                timeout=60,
            )
            response.raise_for_status()
            rows.extend(response.json())
        return rows

    def delete_row(self, table: str, id_column: str, row_id: int) -> List[Dict[str, Any]]:
        response = self.session.delete(
            f"{self.base_url}/{table}",
            params={id_column: f"eq.{int(row_id)}"},
            headers={"Prefer": "return=representation"},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def update_row(self, table: str, id_column: str, row_id: int, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = self.session.patch(
            f"{self.base_url}/{table}",
            params={id_column: f"eq.{int(row_id)}"},
            headers={"Prefer": "return=representation"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete or filter non-filtered master rows in ambiguous domains")
    parser.add_argument("--dry-run", action="store_true", help="Write plan and backup only")
    parser.add_argument(
        "--domains",
        nargs="*",
        help="Optional normalized domains to target. Defaults to all currently ambiguous domains.",
    )
    return parser.parse_args()


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def fetch_all_rows(client: SupabaseRestClient, table: str, select: str, order: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(table, select, limit=PAGE_SIZE, offset=offset, order=order)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text[4:] if text.startswith("www.") else text
    return text.strip(".")


def is_filtered_process_flag(value: Any) -> bool:
    return str(value or "").strip().lower() == "filtered"


def master_domain(row: Dict[str, Any]) -> str:
    return normalize_domain(row.get("website_clean") or row.get("website"))


def build_plan(master_rows: Sequence[Dict[str, Any]], target_domains: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    filtered_by_domain: Dict[str, List[Dict[str, Any]]] = {}
    active_by_domain: Dict[str, List[Dict[str, Any]]] = {}

    for row in master_rows:
        domain = master_domain(row)
        if not domain:
            continue
        if is_filtered_process_flag(row.get("process_flag")):
            filtered_by_domain.setdefault(domain, []).append(row)
        else:
            active_by_domain.setdefault(domain, []).append(row)

    if target_domains:
        ambiguous_domains = sorted({normalize_domain(domain) for domain in target_domains if normalize_domain(domain)})
    else:
        ambiguous_domains = sorted(set(filtered_by_domain) & set(active_by_domain))

    plan: List[Dict[str, Any]] = []
    for domain in ambiguous_domains:
        if domain not in filtered_by_domain or domain not in active_by_domain:
            continue
        filtered_ids = ",".join(str(row.get("id")) for row in filtered_by_domain[domain])
        filtered_names = " | ".join(str(row.get("name") or "").strip() for row in filtered_by_domain[domain])
        for row in active_by_domain[domain]:
            plan.append(
                {
                    "action": "delete_or_mark_filtered_master",
                    "master_id": row.get("id"),
                    "business_id": row.get("business_id"),
                    "domain_name": domain,
                    "name": row.get("name") or "",
                    "website": row.get("website") or "",
                    "website_clean": row.get("website_clean") or "",
                    "current_process_flag": row.get("process_flag") or "",
                    "filtered_master_ids": filtered_ids,
                    "filtered_master_names": filtered_names,
                    "fallback_payload_json": json.dumps({"process_flag": "filtered"}, ensure_ascii=False, sort_keys=True),
                    "detail": "non-filtered master row shares a domain with filtered master row(s)",
                }
            )
    return plan


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    if not fieldnames:
        fieldnames = ["empty"]
        rows = [{"empty": ""}]
    with path.open("w", encoding=OUTPUT_ENCODING, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    client = load_client()

    master_rows = fetch_all_rows(
        client,
        "master_business_info",
        "id,business_id,name,website,website_clean,process_flag",
        "id.asc",
    )
    plan = build_plan(master_rows, args.domains)
    target_ids = sorted({int(row["master_id"]) for row in plan})
    backup_rows = client.fetch_rows_by_ids("master_business_info", "id", target_ids, "*")

    plan_path = OUTPUT_DIR / f"ambiguous_master_filtered_fix_plan_{timestamp}.csv"
    backup_path = OUTPUT_DIR / f"ambiguous_master_filtered_fix_backup_{timestamp}.json"
    summary_path = OUTPUT_DIR / f"ambiguous_master_filtered_fix_execution_{timestamp}.json"

    write_csv(plan_path, plan)
    backup_path.write_text(json.dumps(backup_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = {
        "delete_master_success": 0,
        "mark_filtered_success": 0,
        "errors": 0,
    }
    execution_log: List[Dict[str, Any]] = []

    if not args.dry_run:
        for row in plan:
            row_id = int(row["master_id"])
            try:
                deleted = client.delete_row("master_business_info", "id", row_id)
                counts["delete_master_success"] += 1
                execution_log.append(
                    {
                        "master_id": row_id,
                        "domain_name": row["domain_name"],
                        "status": "deleted",
                        "deleted_count": len(deleted),
                    }
                )
                continue
            except Exception as delete_exc:
                try:
                    payload = json.loads(row["fallback_payload_json"])
                    updated = client.update_row("master_business_info", "id", row_id, payload)
                    counts["mark_filtered_success"] += 1
                    execution_log.append(
                        {
                            "master_id": row_id,
                            "domain_name": row["domain_name"],
                            "status": "marked_filtered",
                            "delete_error": str(delete_exc),
                            "updated_count": len(updated),
                        }
                    )
                except Exception as update_exc:  # pragma: no cover
                    counts["errors"] += 1
                    execution_log.append(
                        {
                            "master_id": row_id,
                            "domain_name": row["domain_name"],
                            "status": "error",
                            "delete_error": str(delete_exc),
                            "update_error": str(update_exc),
                        }
                    )

    verification_rows = client.fetch_rows_by_ids("master_business_info", "id", target_ids, "id,process_flag")
    verification_by_id = {int(row["id"]): row for row in verification_rows}
    unresolved = []
    deleted_ids = []
    filtered_ids = []
    for row in plan:
        row_id = int(row["master_id"])
        remaining = verification_by_id.get(row_id)
        if remaining is None:
            deleted_ids.append(row_id)
            continue
        if is_filtered_process_flag(remaining.get("process_flag")):
            filtered_ids.append(row_id)
            continue
        unresolved.append({"master_id": row_id, "process_flag": remaining.get("process_flag")})

    summary = {
        "dry_run": args.dry_run,
        "plan_path": str(plan_path),
        "backup_path": str(backup_path),
        "target_row_count": len(target_ids),
        "target_domain_count": len({row["domain_name"] for row in plan}),
        "counts": counts,
        "verification": {
            "deleted_count": len(deleted_ids),
            "marked_filtered_or_already_filtered_count": len(filtered_ids),
            "unresolved_count": len(unresolved),
            "deleted_ids": deleted_ids,
            "filtered_ids": filtered_ids,
            "unresolved": unresolved,
        },
        "execution_log": execution_log,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "summary_path": str(summary_path),
                "plan_path": str(plan_path),
                "backup_path": str(backup_path),
                "target_row_count": len(target_ids),
                "target_domain_count": summary["target_domain_count"],
                **counts,
                **summary["verification"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
