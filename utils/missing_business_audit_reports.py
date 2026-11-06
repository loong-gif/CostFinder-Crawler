from __future__ import annotations

import copy
import csv
import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from utils.missing_business_audit_firecrawl import RawSpoolDb

PAGE_SIZE = 1000


def _fetch_all(db: Any, table: str, select: str, *, order: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = db.fetch_rows(
            table,
            select,
            limit=PAGE_SIZE,
            offset=offset,
            order=order,
        )
        rows.extend(dict(row) for row in page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def select_missing_businesses(
    db: Any,
    *,
    business_ids: Sequence[int] | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_businesses = _fetch_all(
        db,
        "master_business_info",
        "business_id,name,website,address,city",
        order="business_id.asc",
    )
    promotions = _fetch_all(
        db,
        "clinic_promotions",
        "business_id",
        order="business_id.asc",
    )
    promoted_ids = {int(row["business_id"]) for row in promotions if row.get("business_id") is not None}
    wanted = {int(value) for value in business_ids or []}
    targets = [
        row
        for row in all_businesses
        if int(row["business_id"]) not in promoted_ids
        and (not wanted or int(row["business_id"]) in wanted)
    ]
    if limit is not None:
        targets = targets[:limit]
    return targets, all_businesses


def build_audit(results: Sequence[dict[str, Any]], *, write_raw: bool) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for row in results:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "run_date": date.today().isoformat(),
        "write_raw": write_raw,
        "businesses": len(results),
        "status_counts": status_counts,
        "balanced": sum(status_counts.values()) == len(results),
        "results": list(results),
    }


def write_reports(audit: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(audit["run_date"]).replace("-", "_")
    json_path = output_dir / f"missing_business_promotion_audit_{stamp}.json"
    csv_path = output_dir / f"missing_business_promotion_audit_{stamp}.csv"
    json_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = (
        "business_id",
        "name",
        "website",
        "host",
        "status",
        "candidate_urls",
        "raw_ids",
        "promotions",
        "errors",
    )
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in audit["results"]:
            writer.writerow(
                {
                    key: (
                        json.dumps(result.get(key), ensure_ascii=False)
                        if isinstance(result.get(key), (list, dict))
                        else result.get(key)
                    )
                    for key in columns
                }
            )
    return json_path, csv_path


def write_raw_spool(db: RawSpoolDb, output_dir: Path, run_date: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(run_date).replace("-", "_")
    path = output_dir / f"missing_business_promotion_raw_{stamp}.json"
    path.write_text(
        json.dumps({"records": db.records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def reconcile_audit_raw_ids(
    audit: dict[str, Any], mapping: Mapping[int, int]
) -> dict[str, Any]:
    """Replace MCP spool IDs with the actual firecrawl_scrape_raw IDs."""
    resolved = copy.deepcopy(audit)

    def replace(promotion: dict[str, Any]) -> None:
        raw_id = promotion.get("raw_id")
        if raw_id is not None and int(raw_id) in mapping:
            promotion["raw_id"] = int(mapping[int(raw_id)])

    for result in resolved.get("results") or []:
        result["raw_ids"] = [
            int(mapping.get(int(raw_id), int(raw_id)))
            for raw_id in result.get("raw_ids") or []
        ]
        for promotion in result.get("promotions") or []:
            replace(promotion)
        for promotion in result.get("unresolved_promotions") or []:
            replace(promotion)
    return resolved
