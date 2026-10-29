#!/usr/bin/env python3
"""Build source-level membership coverage from the resumed page snapshots."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apply_offer_plan_20260918 import fetch_all, load_client  # noqa: E402


SIGNAL = re.compile(
    r"\$|%|\boff\b|\bfree\b|\bunit\b|\bunits\b|\bsyringe\b|\bper treatment\b|\bpackage\b",
    re.IGNORECASE,
)


def candidate_lines(markdown: str) -> list[str]:
    lines = []
    for raw in markdown.splitlines():
        line = " ".join(raw.split())
        if line and len(line) <= 400 and SIGNAL.search(line):
            lines.append(line)
    return lines[:200]


def build_report(source_dir: Path) -> dict:
    snapshot_path = source_dir / "snapshot.json"
    if not snapshot_path.exists():
        snapshot_path = source_dir.parent / "clinic_memberships_plan_20260918_full" / "snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    pages = json.loads((source_dir / "firecrawl_main_scrapes_resumed.json").read_text(encoding="utf-8"))
    page_by_url = {str(row.get("requested_url")): row for row in pages}
    client = load_client()
    memberships = fetch_all(
        client,
        "clinic_memberships",
        "plan_id,business_id,membership_name,source_url,is_offer_extracted,is_ocr_required,need_browser,benefits,updated_at",
        order="plan_id.asc",
    )
    offers = fetch_all(
        client,
        "promo_offer_master",
        "id,business_id,membership_plan_id,is_active,offer_raw_text,promo_offer_items(item_name,service_id)",
        order="id.asc",
    )
    active_by_plan: defaultdict[int, list[dict]] = defaultdict(list)
    for offer in offers:
        if offer.get("is_active") and offer.get("membership_plan_id") is not None:
            active_by_plan[int(offer["membership_plan_id"])].append(offer)
    plans_by_url: defaultdict[str, list[int]] = defaultdict(list)
    for row in memberships:
        plans_by_url[str(row.get("source_url") or "")].append(int(row["plan_id"]))

    source_rows = []
    for url, plan_ids in sorted(plans_by_url.items()):
        page = page_by_url.get(url, {})
        markdown = str(page.get("markdown") or "")
        source_rows.append(
            {
                "source_url": url,
                "plan_ids": plan_ids,
                "plan_count": len(plan_ids),
                "fetch_status": "markdown" if markdown else "empty_page",
                "http_status": page.get("http_status"),
                "scrape_id": page.get("scrape_id"),
                "browser_required": any(bool(row.get("need_browser")) for row in memberships if int(row["plan_id"]) in plan_ids),
                "ocr_required": any(bool(row.get("is_ocr_required")) for row in memberships if int(row["plan_id"]) in plan_ids),
                "candidate_line_count": len(candidate_lines(markdown)),
                "candidate_evidence": candidate_lines(markdown),
                "current_active_offer_count": sum(len(active_by_plan[plan_id]) for plan_id in plan_ids),
                "current_is_offer_extracted": [
                    row.get("is_offer_extracted")
                    for row in memberships
                    if int(row["plan_id"]) in plan_ids
                ],
                "proposed_action": "REVIEW" if not markdown or any(row.get("is_offer_extracted") is not True for row in memberships if int(row["plan_id"]) in plan_ids) else "KEEP",
            }
        )
    statuses = Counter(row["fetch_status"] for row in source_rows)
    unresolved_plans = [
        int(row["plan_id"])
        for row in memberships
        if row.get("is_offer_extracted") is not True
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run",
        "source_artifact": str(source_dir),
        "summary": {
            "membership_count": len(memberships),
            "distinct_source_url_count": len(source_rows),
            "source_status_counts": dict(statuses),
            "source_urls_with_markdown": sum(row["fetch_status"] == "markdown" for row in source_rows),
            "plans_with_unresolved_extraction": len(unresolved_plans),
            "unresolved_plan_ids": unresolved_plans,
            "active_offer_count": sum(len(rows) for rows in active_by_plan.values()),
            "candidate_line_count": sum(row["candidate_line_count"] for row in source_rows),
            "semantic_missing_offer_count": None,
            "llm_status": "unavailable: Gemini project prepayment credits depleted",
        },
        "source_rows": source_rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-artifact-dir",
        type=Path,
        default=ROOT / ".firecrawl" / "clinic_memberships_plan_20260918_resume_scrapes",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs" / "offer_plan_20260918" / "membership_source_coverage_20260918.json",
    )
    args = parser.parse_args()
    report = build_report(args.source_artifact_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
