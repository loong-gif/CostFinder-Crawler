#!/usr/bin/env python3
"""
Delete promo_website_staging rows whose domain maps only to filtered master records.

The script is intentionally conservative:
- process_flag is compared after trim/lowercase.
- domain matching uses the same normalization as the website staging quality fix.
- domains that have both filtered and non-filtered master rows are skipped unless
  --include-ambiguous is passed.
- every target row is backed up before deletion.
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
from urllib.parse import urlsplit

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete promo_website_staging rows mapped to filtered master_business_info records")
    parser.add_argument("--dry-run", action="store_true", help="Write plan and backup only")
    parser.add_argument(
        "--include-ambiguous",
        action="store_true",
        help="Also delete domains that have both filtered and non-filtered master rows",
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


def row_domain(row: Dict[str, Any]) -> str:
    domain = normalize_domain(row.get("domain_name"))
    if domain:
        return domain
    parsed = urlsplit(str(row.get("subpage_url") or "").strip())
    return normalize_domain(parsed.netloc)


def is_filtered_process_flag(value: Any) -> bool:
    return str(value or "").strip().lower() == "filtered"


def master_domain(row: Dict[str, Any]) -> str:
    return normalize_domain(row.get("website_clean") or row.get("website"))


def build_plan(
    staging_rows: Sequence[Dict[str, Any]],
    master_rows: Sequence[Dict[str, Any]],
    *,
    include_ambiguous: bool,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
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

    ambiguous_domains = sorted(set(filtered_by_domain) & set(active_by_domain))
    delete_domains = set(filtered_by_domain)
    if not include_ambiguous:
        delete_domains -= set(ambiguous_domains)

    ambiguous_rows: List[Dict[str, Any]] = []
    for domain in ambiguous_domains:
        ambiguous_rows.append(
            {
                "domain_name": domain,
                "filtered_master_ids": ",".join(str(row.get("id")) for row in filtered_by_domain[domain]),
                "active_master_ids": ",".join(str(row.get("id")) for row in active_by_domain[domain]),
                "filtered_names": " | ".join(str(row.get("name") or "").strip() for row in filtered_by_domain[domain]),
                "active_names": " | ".join(str(row.get("name") or "").strip() for row in active_by_domain[domain]),
            }
        )

    plan: List[Dict[str, Any]] = []
    seen_ids = set()
    for row in staging_rows:
        domain = row_domain(row)
        if not domain or domain not in delete_domains:
            continue
        row_id = int(row["promo_website_id"])
        if row_id in seen_ids:
            continue
        seen_ids.add(row_id)
        filtered_rows = filtered_by_domain[domain]
        plan.append(
            {
                "action": "delete_filtered_master_domain",
                "promo_website_id": row_id,
                "domain_name": domain,
                "subpage_url": row.get("subpage_url") or "",
                "current_name": row.get("name") or "",
                "filtered_master_ids": ",".join(str(item.get("id")) for item in filtered_rows),
                "filtered_business_ids": ",".join(str(item.get("business_id")) for item in filtered_rows if item.get("business_id") is not None),
                "filtered_master_names": " | ".join(str(item.get("name") or "").strip() for item in filtered_rows),
                "detail": "staging domain maps to filtered master_business_info only",
            }
        )
    return plan, ambiguous_rows


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

    plan, ambiguous_rows = build_plan(staging_rows, master_rows, include_ambiguous=args.include_ambiguous)
    target_ids = sorted({int(row["promo_website_id"]) for row in plan})
    backup_rows = client.fetch_rows_by_ids("promo_website_staging", "promo_website_id", target_ids, "*")

    plan_path = OUTPUT_DIR / f"promo_website_staging_filtered_master_delete_plan_{timestamp}.csv"
    ambiguous_path = OUTPUT_DIR / f"promo_website_staging_filtered_master_ambiguous_domains_{timestamp}.csv"
    backup_path = OUTPUT_DIR / f"promo_website_staging_filtered_master_delete_backup_{timestamp}.json"
    summary_path = OUTPUT_DIR / f"promo_website_staging_filtered_master_delete_execution_{timestamp}.json"

    write_csv(plan_path, plan)
    write_csv(ambiguous_path, ambiguous_rows)
    backup_path.write_text(json.dumps(backup_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = {"delete_filtered_master_domain": 0, "errors": 0}
    execution_log: List[Dict[str, Any]] = []

    if not args.dry_run and target_ids:
        for row in plan:
            row_id = int(row["promo_website_id"])
            try:
                client.delete_rows("promo_website_staging", "promo_website_id", [row_id])
                counts["delete_filtered_master_domain"] += 1
                execution_log.append({"action": row["action"], "promo_website_id": row_id, "domain_name": row["domain_name"], "status": "applied"})
            except Exception as exc:  # pragma: no cover
                counts["errors"] += 1
                execution_log.append({"action": row["action"], "promo_website_id": row_id, "domain_name": row["domain_name"], "status": "error", "error": str(exc)})

    remaining_rows = client.fetch_rows_by_ids("promo_website_staging", "promo_website_id", target_ids, "promo_website_id")
    remaining_ids = sorted(int(row["promo_website_id"]) for row in remaining_rows)

    summary = {
        "dry_run": args.dry_run,
        "include_ambiguous": args.include_ambiguous,
        "plan_path": str(plan_path),
        "ambiguous_path": str(ambiguous_path),
        "backup_path": str(backup_path),
        "target_row_count": len(target_ids),
        "target_domain_count": len({row["domain_name"] for row in plan}),
        "ambiguous_domain_count": len(ambiguous_rows),
        "counts": counts,
        "verification": {
            "deleted_ids_still_present_count": len(remaining_ids),
            "deleted_ids_still_present": remaining_ids,
        },
        "execution_log_sample": execution_log[:50],
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "summary_path": str(summary_path),
                "plan_path": str(plan_path),
                "ambiguous_path": str(ambiguous_path),
                "backup_path": str(backup_path),
                "target_row_count": len(target_ids),
                "target_domain_count": summary["target_domain_count"],
                "ambiguous_domain_count": len(ambiguous_rows),
                **counts,
                **summary["verification"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
