#!/usr/bin/env python3
"""Export reviewed upstream sources that still have no directly linked Offer."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from extract_high_confidence_upstream_offers import MEMBERSHIP_IDS, PROMOTION_IDS
from utils.supabase_access import fetch_all_rows, load_supabase_client

OUT = ROOT / "outputs" / "pending_upstream_sources_20260907.json"


def main() -> int:
    client = load_supabase_client(ROOT)
    promotions = fetch_all_rows(client, "clinic_promotions", "promotion_id,business_id,promotion_title,promotion_content,source_url,campaign_end_date,is_offer_extracted", order="promotion_id.asc")
    memberships = fetch_all_rows(client, "clinic_memberships", "plan_id,business_id,membership_name,benefits,source_url,is_offer_extracted", order="plan_id.asc")
    offers = fetch_all_rows(client, "promo_offer_master", "promotion_id,membership_plan_id", order="id.asc")
    promo_counts: dict[int, int] = {}
    member_counts: dict[int, int] = {}
    for offer in offers:
        if offer.get("promotion_id") is not None:
            promo_counts[int(offer["promotion_id"])] = promo_counts.get(int(offer["promotion_id"]), 0) + 1
        if offer.get("membership_plan_id") is not None:
            member_counts[int(offer["membership_plan_id"])] = member_counts.get(int(offer["membership_plan_id"]), 0) + 1

    rows = []
    for row in promotions:
        source_id = int(row["promotion_id"])
        if source_id in PROMOTION_IDS and not promo_counts.get(source_id):
            rows.append({
                "source_type": "Promotion",
                "source_id": source_id,
                "business_id": row.get("business_id"),
                "source_name": row.get("promotion_title") or "",
                "campaign_end_date": row.get("campaign_end_date"),
                "is_offer_extracted": row.get("is_offer_extracted"),
                "source_url": row.get("source_url") or "",
                "source_text": row.get("promotion_content") or "",
                "current_offer_count": 0,
                "review_status": "待复核：当前解析器未生成 Offer",
            })
    for row in memberships:
        source_id = int(row["plan_id"])
        if source_id in MEMBERSHIP_IDS and not member_counts.get(source_id):
            rows.append({
                "source_type": "Membership",
                "source_id": source_id,
                "business_id": row.get("business_id"),
                "source_name": row.get("membership_name") or "",
                "campaign_end_date": "",
                "is_offer_extracted": row.get("is_offer_extracted"),
                "source_url": row.get("source_url") or "",
                "source_text": row.get("benefits") or "",
                "current_offer_count": 0,
                "review_status": "待复核：当前解析器未生成 Offer",
            })
    rows.sort(key=lambda r: (r["source_type"], r["source_id"]))
    OUT.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "row_count": len(rows), "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(OUT)
    print(json.dumps({"rows": len(rows), "promotions": sum(r["source_type"] == "Promotion" for r in rows), "memberships": sum(r["source_type"] == "Membership" for r in rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
