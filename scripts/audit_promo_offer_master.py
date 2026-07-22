#!/usr/bin/env python3
"""Audit promo_offer_master data quality from Supabase (live schema)."""
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

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING  # noqa: E402
from utils.promo_offer_audit import (  # noqa: E402
    AuditIssue,
    build_exact_duplicate_groups,
    build_fingerprint_duplicate_groups,
    build_offer_text_duplicate_groups,
    audit_rows,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key  # noqa: E402

TABLE_NAME = "promo_offer_master"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"
DEFAULT_RESULT_DIR = PROJECT_ROOT / "output" / "results"
PAGE_SIZE = 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit promo_offer_master data quality")
    parser.add_argument("--limit", type=int, default=None, help="只拉取前 N 条记录用于快速检查")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="报告输出目录")
    parser.add_argument("--result-dir", default=str(DEFAULT_RESULT_DIR), help="结构化结果输出目录")
    return parser.parse_args()


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = get_supabase_writer_key()
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 writer key")
    return SupabaseRestClient(base_url, service_role_key)


def fetch_all_rows(client: SupabaseRestClient, *, limit: Optional[int]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    remaining = limit
    while True:
        page_limit = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
        batch = client.fetch_rows(
            TABLE_NAME,
            "*",
            limit=page_limit,
            offset=offset,
            order="id.asc",
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_limit:
            break
        offset += page_limit
        if remaining is not None:
            remaining -= len(batch)
            if remaining <= 0:
                break
    return rows


def fetch_lookup_sets(client: SupabaseRestClient) -> tuple[set, set, dict]:
    master_rows = client.fetch_rows("master_business_info", "business_id", limit=5000)
    plan_rows = client.fetch_rows("clinic_memberships", "plan_id", limit=5000)
    service_rows = client.fetch_rows("clinic_services", "service_id,business_id,service_name", limit=5000)
    master_ids = {row["business_id"] for row in master_rows if row.get("business_id") is not None}
    plan_ids = {row["plan_id"] for row in plan_rows if row.get("plan_id") is not None}
    svc_by_id = {row["service_id"]: row for row in service_rows if row.get("service_id") is not None}
    return master_ids, plan_ids, svc_by_id


def write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=OUTPUT_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_report(
    *,
    summary: Dict[str, Any],
    exact_duplicates: List[Dict[str, Any]],
    text_duplicates: List[Dict[str, Any]],
    fp_duplicates: List[Dict[str, Any]],
    issues: List[AuditIssue],
    alignment_rows: List[Dict[str, Any]],
    paths: Dict[str, Path],
) -> str:
    issue_counter = Counter(issue.issue_type for issue in issues)
    active_counter = Counter(
        issue.issue_type for issue in issues if issue.status == "active"
    )
    top_issue_lines = "\n".join(
        f"- `{issue}`: {count} (active: {active_counter.get(issue, 0)})"
        for issue, count in issue_counter.most_common(20)
    )
    top_noncanonical = Counter(
        row["aligned_service_name_canonical"]
        for row in alignment_rows
        if row["aligned_service_name_canonical"]
        and row["aligned_service_name_canonical"] != row["service_name"]
        and row["status"] == "active"
    )
    top_alignment_lines = "\n".join(
        f"- `{name}`: {count}" for name, count in top_noncanonical.most_common(12)
    )
    exact_dup_lines = "\n".join(
        f"- `{item['source_name']}` / `{item['service_name']}`: {item['count']} rows"
        for item in list(exact_duplicates)[:10]
    )
    text_dup_lines = "\n".join(
        f"- `{item['source_name']}`: {item['count']} rows ({item.get('active_count', 0)} active)"
        for item in list(text_duplicates)[:10]
    )
    fp_dup_lines = "\n".join(
        f"- fingerprint `{item['offer_fingerprint'][:12]}...`: {item['count']} active rows"
        for item in list(fp_duplicates)[:10]
    )
    return f"""# promo_offer_master Data Quality Audit

- Total rows audited: `{summary['row_count']}`
- Status breakdown: `{summary['status_counts']}`
- Total issues flagged: `{summary['total_issues']}`
- Active issues: `{summary['active_issue_count']}` (high: `{summary['active_high_severity_count']}`)

## Top Issue Types (all / active)
{top_issue_lines or "- None"}

## Duplicate Findings
### Exact Duplicates
{exact_dup_lines or "- None"}

### Potential Duplicates By offer_raw_text
{text_dup_lines or "- None"}

### Active Fingerprint Duplicates
{fp_dup_lines or "- None"}

## Service Name Alignment (active)
- Rows needing manual service_name review: `{sum(1 for row in alignment_rows if row['needs_manual_review'] == 'TRUE' and row['status'] == 'active')}`
- Rows with non-canonical service_name: `{sum(1 for row in alignment_rows if row['aligned_service_name_canonical'] and row['aligned_service_name_canonical'] != row['service_name'] and row['status'] == 'active')}`

### Top Suggested Canonical Service Entities
{top_alignment_lines or "- None"}

## Output Files
- Summary JSON: `{paths['summary']}`
- Issues CSV: `{paths['issues']}`
- Exact duplicates CSV: `{paths['exact']}`
- offer_raw_text duplicate CSV: `{paths['text']}`
- Fingerprint duplicate CSV: `{paths['fingerprint']}`
- Service alignment CSV: `{paths['alignment']}`
"""


def main() -> None:
    args = parse_args()
    report_dir = Path(args.report_dir).expanduser().resolve()
    result_dir = Path(args.result_dir).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    client = load_supabase_client()
    rows = fetch_all_rows(client, limit=args.limit)
    master_ids, plan_ids, svc_by_id = fetch_lookup_sets(client)

    exact_duplicates = build_exact_duplicate_groups(rows)
    text_duplicates = build_offer_text_duplicate_groups(rows)
    fp_duplicates = build_fingerprint_duplicate_groups(rows)
    issues, alignment_rows, layered_summary = audit_rows(
        rows,
        master_business_ids=master_ids,
        membership_plan_ids=plan_ids,
        service_rows=svc_by_id,
    )

    issues_rows = [issue.as_row() for issue in issues]
    exact_dup_rows = [
        {
            "count": item["count"],
            "source_name": item["source_name"],
            "service_name": item["service_name"],
            "offer_raw_text": item["offer_raw_text"],
            "ids": json.dumps(item["ids"], ensure_ascii=False),
        }
        for item in exact_duplicates
    ]
    text_dup_rows = [
        {
            "count": item["count"],
            "active_count": item.get("active_count", 0),
            "source_name": item["source_name"],
            "normalized_offer_raw_text": item["normalized_offer_raw_text"],
            "service_names": json.dumps(item["service_names"], ensure_ascii=False),
            "ids": json.dumps(item["ids"], ensure_ascii=False),
        }
        for item in text_duplicates
    ]
    fp_dup_rows = [
        {
            "count": item["count"],
            "offer_fingerprint": item["offer_fingerprint"],
            "ids": json.dumps(item["ids"], ensure_ascii=False),
        }
        for item in fp_duplicates
    ]

    summary = {
        "table": TABLE_NAME,
        "audited_at": datetime.now().isoformat(),
        "exact_duplicate_groups": len(exact_duplicates),
        "exact_duplicate_rows": sum(item["count"] for item in exact_duplicates),
        "offer_raw_text_duplicate_groups": len(text_duplicates),
        "offer_raw_text_duplicate_rows": sum(item["count"] for item in text_duplicates),
        "active_fingerprint_duplicate_groups": len(fp_duplicates),
        "rows_needing_service_manual_review_active": sum(
            1
            for row in alignment_rows
            if row["needs_manual_review"] == "TRUE" and row["status"] == "active"
        ),
        **layered_summary,
    }

    summary_path = result_dir / f"promo_offer_master_audit_summary_{timestamp}.json"
    issues_path = result_dir / f"promo_offer_master_audit_issues_{timestamp}.csv"
    exact_duplicates_path = result_dir / f"promo_offer_master_exact_duplicates_{timestamp}.csv"
    text_duplicates_path = result_dir / f"promo_offer_master_offer_text_duplicates_{timestamp}.csv"
    fingerprint_path = result_dir / f"promo_offer_master_fingerprint_duplicates_{timestamp}.csv"
    alignment_path = result_dir / f"promo_offer_master_service_alignment_{timestamp}.csv"
    report_path = report_dir / f"promo_offer_master_audit_{timestamp}.md"

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(
        issues_path,
        issues_rows,
        ["id", "status", "source_name", "service_name", "issue_type", "severity", "detail"],
    )
    write_csv(
        exact_duplicates_path,
        exact_dup_rows,
        ["count", "source_name", "service_name", "offer_raw_text", "ids"],
    )
    write_csv(
        text_duplicates_path,
        text_dup_rows,
        ["count", "active_count", "source_name", "normalized_offer_raw_text", "service_names", "ids"],
    )
    write_csv(fingerprint_path, fp_dup_rows, ["count", "offer_fingerprint", "ids"])
    write_csv(
        alignment_path,
        alignment_rows,
        [
            "id",
            "status",
            "source_name",
            "service_name",
            "aligned_service_name_canonical",
            "aligned_service_category",
            "needs_manual_review",
            "alignment_confidence",
            "alignment_note",
        ],
    )

    paths = {
        "summary": summary_path,
        "issues": issues_path,
        "exact": exact_duplicates_path,
        "text": text_duplicates_path,
        "fingerprint": fingerprint_path,
        "alignment": alignment_path,
    }
    report = build_report(
        summary=summary,
        exact_duplicates=exact_duplicates,
        text_duplicates=text_duplicates,
        fp_duplicates=fp_duplicates,
        issues=issues,
        alignment_rows=alignment_rows,
        paths=paths,
    )
    report_path.write_text(report, encoding="utf-8")

    print(
        json.dumps(
            {
                "summary_path": str(summary_path),
                "report_path": str(report_path),
                "row_count": len(rows),
                "total_issues": len(issues),
                "active_high_severity": summary["active_high_severity_count"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
