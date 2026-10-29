#!/usr/bin/env python3
"""Export the frozen live-QA unverifiable queue as a reviewable CSV."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROMOTION_REPORT = ROOT / "clinic_promotions_live_qa.json"
MEMBERSHIP_REPORT = ROOT / "clinic_memberships_live_qa.json"
OUTPUT = ROOT / "unverifiable_live_records_20260913.csv"


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def main() -> None:
    rows: list[dict[str, str]] = []
    promotion = json.loads(PROMOTION_REPORT.read_text(encoding="utf-8"))
    promotion_statuses = {"insufficient_evidence", "page_unavailable", "blocked", "image_only"}
    for record in promotion.get("records") or []:
        status = record.get("status") or record.get("qa_status")
        if status not in promotion_statuses:
            continue
        rows.append(
            {
                "table": "clinic_promotions",
                "record_id": text(record.get("record_id")),
                "business_id": text(record.get("business_id")),
                "name_or_title": text(record.get("name_or_title") or record.get("promotion_title")),
                "source_url": text(record.get("source_url")),
                "requested_url": text(record.get("requested_url")),
                "final_url": text(record.get("final_url")),
                "qa_status": text(status),
                "blocking_category": "image_or_ocr" if status == "image_only" else status,
                "confidence": text(record.get("confidence")),
                "difference": text(record.get("difference")),
                "evidence_text": text(record.get("evidence_text")),
                "evidence_type": text(record.get("evidence_type")),
                "recommended_action": text(record.get("recommended_action")),
                "is_ocr_required": text(record.get("is_ocr_required")),
                "checked_at": text(record.get("checked_at")),
            }
        )

    membership = json.loads(MEMBERSHIP_REPORT.read_text(encoding="utf-8"))
    membership_statuses = {"insufficient_evidence", "page_unavailable", "blocked"}
    for record in membership.get("record_evidence") or []:
        status = record.get("qa_status")
        if status not in membership_statuses:
            continue
        rows.append(
            {
                "table": "clinic_memberships",
                "record_id": text(record.get("record_id")),
                "business_id": text(record.get("business_id")),
                "name_or_title": text(record.get("name_or_title") or record.get("membership_name")),
                "source_url": text(record.get("source_url")),
                "requested_url": text(record.get("requested_url")),
                "final_url": text(record.get("final_url")),
                "qa_status": text(status),
                "blocking_category": status,
                "confidence": text(record.get("confidence")),
                "difference": text(record.get("difference")),
                "evidence_text": text(record.get("evidence_text")),
                "evidence_type": text(record.get("evidence_type")),
                "recommended_action": text(record.get("recommended_action")),
                "is_ocr_required": text(record.get("is_ocr_required")),
                "checked_at": text(record.get("checked_at")),
            }
        )

    fieldnames = [
        "table", "record_id", "business_id", "name_or_title", "source_url",
        "requested_url", "final_url", "qa_status", "blocking_category",
        "confidence", "difference", "evidence_text", "evidence_type",
        "recommended_action", "is_ocr_required", "checked_at",
    ]
    with OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"output": str(OUTPUT), "records": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
