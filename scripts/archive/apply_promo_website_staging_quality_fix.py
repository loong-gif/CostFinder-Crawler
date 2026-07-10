#!/usr/bin/env python3
"""
Apply safe data quality fixes for promo_website_staging:
1) delete empty staging rows,
2) delete duplicate normalized URLs,
3) backfill missing name from master_business_info by normalized domain.
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
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING  # noqa: E402


OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 1000
TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "gbraid",
    "wbraid",
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

    def delete_rows(self, table: str, id_column: str, ids: Sequence[int]) -> List[Dict[str, Any]]:
        if not ids:
            return []
        deleted: List[Dict[str, Any]] = []
        unique_ids = sorted(set(int(item) for item in ids))
        for start in range(0, len(unique_ids), 100):
            chunk = unique_ids[start : start + 100]
            response = self.session.delete(
                f"{self.base_url}/{table}",
                params={id_column: f"in.({','.join(str(item) for item in chunk)})"},
                headers={"Prefer": "return=representation"},
                timeout=60,
            )
            response.raise_for_status()
            deleted.extend(response.json())
        return deleted

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
    parser = argparse.ArgumentParser(description="Apply safe promo_website_staging quality fixes")
    parser.add_argument("--dry-run", action="store_true", help="Write plan and backup only")
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


def normalize_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    scheme = "https"
    netloc = normalize_domain(parsed.netloc)
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_items = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() not in TRACKING_QUERY_KEYS]
    return urlunsplit((scheme, netloc, path, urlencode(query_items, doseq=True), ""))


def is_empty_staging_row(row: Dict[str, Any]) -> bool:
    return not str(row.get("subpage_url") or "").strip() and not str(row.get("domain_name") or "").strip() and not str(row.get("page_content") or "").strip()


def is_filtered_process_flag(value: Any) -> bool:
    return str(value or "").strip().lower() == "filtered"


def timestamp_sort_key(row: Dict[str, Any]) -> str:
    return str(row.get("crawl_timestamp") or "")


def duplicate_keep_key(row: Dict[str, Any]) -> Tuple[int, int, int, str, int]:
    has_name = 1 if str(row.get("name") or "").strip() else 0
    has_processed = 1 if str(row.get("processed_status") or "").strip().lower() == "true" else 0
    content_len = len(str(row.get("page_content") or ""))
    # Later timestamp and lower id are tie breakers after quality indicators.
    return (has_name, has_processed, content_len, timestamp_sort_key(row), -int(row.get("promo_website_id") or 0))


def build_master_name_by_domain(master_rows: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    by_domain: Dict[str, str] = {}
    for row in master_rows:
        if is_filtered_process_flag(row.get("process_flag")):
            continue
        domain = normalize_domain(row.get("website_clean") or row.get("website"))
        name = str(row.get("name") or "").strip()
        if not domain or not name or domain in by_domain:
            continue
        by_domain[domain] = name
    return by_domain


def build_plan(staging_rows: Sequence[Dict[str, Any]], master_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    plan: List[Dict[str, Any]] = []
    delete_ids = set()

    for row in staging_rows:
        if is_empty_staging_row(row):
            row_id = int(row["promo_website_id"])
            delete_ids.add(row_id)
            plan.append(
                {
                    "action": "delete_empty_row",
                    "promo_website_id": row_id,
                    "detail": "missing subpage_url, domain_name, and page_content",
                }
            )

    rows_by_url: Dict[str, List[Dict[str, Any]]] = {}
    for row in staging_rows:
        normalized_url = normalize_url(row.get("subpage_url"))
        if not normalized_url:
            continue
        rows_by_url.setdefault(normalized_url, []).append(row)

    for normalized_url, group in rows_by_url.items():
        if len(group) < 2:
            continue
        keep = max(group, key=duplicate_keep_key)
        keep_id = int(keep["promo_website_id"])
        for row in group:
            row_id = int(row["promo_website_id"])
            if row_id == keep_id or row_id in delete_ids:
                continue
            delete_ids.add(row_id)
            plan.append(
                {
                    "action": "delete_duplicate_normalized_url",
                    "promo_website_id": row_id,
                    "keep_id": keep_id,
                    "normalized_subpage_url": normalized_url,
                    "detail": f"duplicate URL group kept id={keep_id}",
                }
            )

    master_name_by_domain = build_master_name_by_domain(master_rows)
    for row in staging_rows:
        row_id = int(row["promo_website_id"])
        if row_id in delete_ids:
            continue
        if str(row.get("name") or "").strip():
            continue
        domain = normalize_domain(row.get("domain_name")) or normalize_domain(urlsplit(str(row.get("subpage_url") or "")).netloc)
        name = master_name_by_domain.get(domain)
        if not name:
            continue
        plan.append(
            {
                "action": "update_name_from_master_domain",
                "promo_website_id": row_id,
                "domain_name": domain,
                "payload_json": json.dumps({"name": name}, ensure_ascii=False, sort_keys=True),
                "detail": f"name -> {name}",
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

    staging_rows = fetch_all_rows(
        client,
        "promo_website_staging",
        "promo_website_id,crawl_timestamp,subpage_url,page_content,domain_name,processed_status,name",
        "promo_website_id.asc",
    )
    master_rows = fetch_all_rows(
        client,
        "master_business_info",
        "id,business_id,name,website,website_clean,process_flag",
        "id.asc",
    )

    plan = build_plan(staging_rows, master_rows)
    target_ids = sorted({int(row["promo_website_id"]) for row in plan if row.get("promo_website_id")})
    backup_rows = client.fetch_rows_by_ids("promo_website_staging", "promo_website_id", target_ids, "*")

    plan_path = OUTPUT_DIR / f"promo_website_staging_quality_fix_plan_{timestamp}.csv"
    backup_path = OUTPUT_DIR / f"promo_website_staging_quality_fix_backup_{timestamp}.json"
    summary_path = OUTPUT_DIR / f"promo_website_staging_quality_fix_execution_{timestamp}.json"

    write_csv(plan_path, plan)
    backup_path.write_text(json.dumps(backup_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = {
        "delete_empty_row": 0,
        "delete_duplicate_normalized_url": 0,
        "update_name_from_master_domain": 0,
        "errors": 0,
    }
    execution_log: List[Dict[str, Any]] = []

    if not args.dry_run:
        for row in plan:
            action = row["action"]
            row_id = int(row["promo_website_id"])
            try:
                if action in {"delete_empty_row", "delete_duplicate_normalized_url"}:
                    client.delete_rows("promo_website_staging", "promo_website_id", [row_id])
                    counts[action] += 1
                    execution_log.append({"action": action, "promo_website_id": row_id, "status": "applied"})
                elif action == "update_name_from_master_domain":
                    payload = json.loads(row["payload_json"])
                    client.update_row("promo_website_staging", "promo_website_id", row_id, payload)
                    counts[action] += 1
                    execution_log.append({"action": action, "promo_website_id": row_id, "payload": payload, "status": "applied"})
            except Exception as exc:  # pragma: no cover
                counts["errors"] += 1
                execution_log.append({"action": action, "promo_website_id": row_id, "status": "error", "error": str(exc)})

    remaining_rows = client.fetch_rows_by_ids("promo_website_staging", "promo_website_id", target_ids, "promo_website_id,name")
    remaining_by_id = {int(row["promo_website_id"]): row for row in remaining_rows}
    verification = {
        "deleted_ids_still_present": [],
        "name_update_mismatches": [],
    }
    for row in plan:
        action = row["action"]
        row_id = int(row["promo_website_id"])
        if action in {"delete_empty_row", "delete_duplicate_normalized_url"}:
            if row_id in remaining_by_id:
                verification["deleted_ids_still_present"].append(row_id)
        elif action == "update_name_from_master_domain":
            expected_name = json.loads(row["payload_json"]).get("name")
            actual_name = (remaining_by_id.get(row_id) or {}).get("name")
            if actual_name != expected_name:
                verification["name_update_mismatches"].append({"promo_website_id": row_id, "expected": expected_name, "actual": actual_name})

    summary = {
        "dry_run": args.dry_run,
        "plan_path": str(plan_path),
        "backup_path": str(backup_path),
        "counts": counts,
        "target_row_count": len(target_ids),
        "verification": {
            "deleted_ids_still_present_count": len(verification["deleted_ids_still_present"]),
            "name_update_mismatch_count": len(verification["name_update_mismatches"]),
        },
        "verification_details": verification,
        "execution_log_sample": execution_log[:50],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary_path": str(summary_path), "plan_path": str(plan_path), "backup_path": str(backup_path), **counts, **summary["verification"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
