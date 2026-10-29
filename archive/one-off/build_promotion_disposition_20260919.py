#!/usr/bin/env python3
"""Build the all-row clinic promotion disposition and source coverage report."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.supabase_access import fetch_all_rows, load_supabase_client


OUTPUT = ROOT / "outputs" / "promotion_disposition_20260919.json"
EVIDENCE_FILES = {
    "visibility": "outputs/final_visibility_gate_audit_20260918.json",
    "p0_p1": "outputs/promotion_p0_p1_browser_review_20260919.json",
    "missing_coverage": "outputs/promotion_missing_coverage_browser_review_20260919.json",
}
EMPTY_CONTENT_IDS = {144, 613, 837, 865, 908, 990, 3460, 3504, 3539, 3548, 3594}
PRICING_ONLY_IDS = {3553, 3554, 3555, 3557, 3558, 3559, 3562, 3572, 3579, 3581, 3583}


def add_evidence(
    evidence: dict[int, list[dict[str, Any]]],
    promotion_id: int,
    source: str,
    status: str | None,
    final_url: str | None = None,
    title: str | None = None,
    claim_count: int | None = None,
    matched_count: int | None = None,
) -> None:
    item = {"source": source, "status": status}
    if final_url:
        item["final_url"] = final_url
    if title:
        item["title"] = title
    if claim_count is not None:
        item["claim_count"] = claim_count
    if matched_count is not None:
        item["matched_count"] = matched_count
    evidence.setdefault(promotion_id, [])
    if item not in evidence[promotion_id]:
        evidence[promotion_id].append(item)


def load_evidence() -> dict[int, list[dict[str, Any]]]:
    evidence: dict[int, list[dict[str, Any]]] = {}
    for key, filename in EVIDENCE_FILES.items():
        data = json.loads((ROOT / filename).read_text())
        if key == "visibility":
            for record in data["records"]:
                add_evidence(
                    evidence,
                    int(record["promotion_id"]),
                    filename,
                    record.get("status"),
                    claim_count=record.get("claim_count"),
                )
            continue

        for result in data["results"].values():
            records = result.get("records")
            if records:
                for record in records:
                    claim_results = record.get("claim_results") or []
                    counts = claim_results[0] if claim_results else {}
                    add_evidence(
                        evidence,
                        int(record["promotion_id"]),
                        filename,
                        result.get("status"),
                        result.get("final_url"),
                        result.get("title"),
                        record.get("claim_count") or counts.get("claim_count"),
                        record.get("matched_count") or counts.get("matched_count"),
                    )
            elif result.get("promotion_id") is not None:
                add_evidence(
                    evidence,
                    int(result["promotion_id"]),
                    filename,
                    result.get("status"),
                    result.get("final_url"),
                    result.get("title"),
                    result.get("claim_count"),
                    result.get("matched_count"),
                )
    return evidence


def main() -> int:
    load_dotenv(ROOT / ".env")
    client = load_supabase_client()
    rows = fetch_all_rows(
        client,
        "clinic_promotions",
        "promotion_id,source_url,is_active,promotion_content,campaign_start_date,campaign_end_date,requires_cf_verification",
        order="promotion_id.asc",
    )
    evidence = load_evidence()
    previous = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else {"records": []}
    previous_by_id = {int(row["promotion_id"]): row for row in previous.get("records", [])}

    records: list[dict[str, Any]] = []
    for row in rows:
        promotion_id = int(row["promotion_id"])
        current_evidence = evidence.get(promotion_id, [])
        if promotion_id in PRICING_ONLY_IDS:
            disposition = "MOVE_TO_SERVICE"
            reason = "Source is a regular pricing/services page, not a promotion; all linked offers are inactive."
        elif promotion_id in EMPTY_CONTENT_IDS:
            disposition = "DELETE_CANDIDATE"
            reason = "P0 source was unavailable, redirected to an unrelated page, or no longer showed a specific promotion."
        elif promotion_id == 162:
            disposition = "SUPERSEDED"
            reason = "Current page is an old specials page; the row was active despite stale historical content."
        elif promotion_id in {789, 810}:
            disposition = "INVALID_SOURCE"
            reason = "Current page is generic or no longer shows the stored promotion claims."
        elif promotion_id == 3588:
            disposition = "UPDATE"
            reason = "Current page exposes only generic September specials text; stale detailed claims were removed and manual verification retained."
        elif promotion_id in {3589, 3597}:
            disposition = "UPDATE"
            reason = "Current source facts were synchronized; stale or ordinary-store offer rows were deactivated."
        else:
            old = previous_by_id.get(promotion_id, {})
            disposition = old.get("disposition", "KEEP")
            reason = old.get("reason")

        records.append(
            {
                "promotion_id": promotion_id,
                "current_in_db": True,
                "source_url": row.get("source_url"),
                "is_active": row.get("is_active"),
                "promotion_content_empty": not bool(row.get("promotion_content")),
                "campaign_start_date": row.get("campaign_start_date"),
                "campaign_end_date": row.get("campaign_end_date"),
                "requires_cf_verification": row.get("requires_cf_verification"),
                "disposition": disposition,
                "reason": reason,
                "evidence": current_evidence,
                "evidence_covered": bool(current_evidence),
            }
        )

    live_ids = {int(row["promotion_id"]) for row in rows}
    for old_record in previous.get("records", []):
        promotion_id = int(old_record["promotion_id"])
        if promotion_id in live_ids or old_record.get("disposition") != "DELETE_CANDIDATE":
            continue
        deleted_record = dict(old_record)
        deleted_record["current_in_db"] = False
        deleted_record["promotion_content_empty"] = True
        records.append(deleted_record)
    records.sort(key=lambda record: int(record["promotion_id"]))

    summary: dict[str, int] = {}
    for record in records:
        summary[record["disposition"]] = summary.get(record["disposition"], 0) + 1
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_snapshot": "outputs/clinic_promotions_snapshot_20260919.json",
        "browser_review": list(EVIDENCE_FILES.values()),
        "scope": {
            "rows_audited": len(records),
            "current_rows": len(rows),
            "deleted_after_disposition": sum(not r["current_in_db"] for r in records),
            "source_evidence_covered": sum(r["evidence_covered"] for r in records),
        },
        "summary": summary,
        "empty_content_ids_before_disposition": sorted(EMPTY_CONTENT_IDS),
        "records": records,
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"rows_audited": len(records), "summary": summary, "missing_evidence": [r["promotion_id"] for r in records if not r["evidence_covered"]]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
