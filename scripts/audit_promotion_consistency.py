#!/usr/bin/env python3
"""Audit clinic promotion lifecycle and offer relationship invariants."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key


def fetch_all(client: SupabaseRestClient, table: str, select: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.fetch_rows(table, select, limit=1000, offset=offset)
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += len(page)


def consistency_counts(
    promotions: list[dict[str, Any]],
    offers: list[dict[str, Any]],
    *,
    as_of: date,
) -> dict[str, Any]:
    offers_by_promotion: dict[int, list[dict[str, Any]]] = defaultdict(list)
    active_fingerprints: dict[str, list[int]] = defaultdict(list)
    for offer in offers:
        promotion_id = offer.get("promotion_id")
        if promotion_id is not None:
            offers_by_promotion[int(promotion_id)].append(offer)
        if offer.get("is_active") and offer.get("offer_fingerprint"):
            active_fingerprints[str(offer["offer_fingerprint"])].append(int(offer["id"]))

    false_with_offer: list[int] = []
    true_without_offer: list[int] = []
    active_expired: list[int] = []
    empty_evidence: list[int] = []
    inactive_with_active_offer: list[int] = []
    null_ocr: list[int] = []
    for promotion in promotions:
        promotion_id = int(promotion["promotion_id"])
        related_offers = offers_by_promotion.get(promotion_id, [])
        has_offer = bool(related_offers)
        if promotion.get("is_offer_extracted") is False and has_offer:
            false_with_offer.append(promotion_id)
        if promotion.get("is_offer_extracted") is True and not has_offer:
            true_without_offer.append(promotion_id)
        end_date = promotion.get("campaign_end_date")
        if promotion.get("is_active") and end_date and date.fromisoformat(end_date) < as_of:
            active_expired.append(promotion_id)
        if not promotion.get("promotion_content"):
            empty_evidence.append(promotion_id)
        if promotion.get("is_ocr_required") is None:
            null_ocr.append(promotion_id)
        if not promotion.get("is_active") and any(offer.get("is_active") for offer in related_offers):
            inactive_with_active_offer.append(promotion_id)

    duplicate_fingerprints = {
        fingerprint: ids
        for fingerprint, ids in active_fingerprints.items()
        if len(ids) > 1
    }
    return {
        "promotion_count": len(promotions),
        "offer_count": len(offers),
        "as_of": as_of.isoformat(),
        "is_offer_extracted_false_with_offer": false_with_offer,
        "is_offer_extracted_true_without_offer": true_without_offer,
        "active_promotion_expired": active_expired,
        "empty_promotion_content": empty_evidence,
        "null_is_ocr_required": null_ocr,
        "inactive_promotion_with_active_offer": inactive_with_active_offer,
        "active_offer_fingerprint_duplicates": duplicate_fingerprints,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--as-of", default=date.today().isoformat())
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    client = SupabaseRestClient(os.environ["SUPABASE_URL"], get_supabase_writer_key())
    promotions = fetch_all(
        client,
        "clinic_promotions",
        "promotion_id,is_active,campaign_end_date,is_offer_extracted,is_ocr_required,promotion_content",
    )
    offers = fetch_all(client, "promo_offer_master", "id,promotion_id,is_active,offer_fingerprint")
    report = consistency_counts(promotions, offers, as_of=date.fromisoformat(args.as_of))
    report["passed"] = all(
        not report[key]
        for key in (
            "is_offer_extracted_false_with_offer",
            "is_offer_extracted_true_without_offer",
            "active_promotion_expired",
            "empty_promotion_content",
            "null_is_ocr_required",
            "inactive_promotion_with_active_offer",
            "active_offer_fingerprint_duplicates",
        )
    )
    output = ROOT / "outputs" / "promotion_consistency_qa_20260919.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
