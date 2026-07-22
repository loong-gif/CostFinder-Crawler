"""Backfill clinic_memberships.benefits + source_url for LOU LOU (business_id=1181)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.schema_contract import TABLE_CLINIC_MEMBERSHIPS, TABLE_CLINIC_PROMOTIONS
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key

BUSINESS_ID = 1181
SOURCE_URL = "https://louloumedspa.com/membership"
AUDIT_PATH = PROJECT_ROOT / ".firecrawl/master-business-search/loulou-membership-backfill-audit.json"


def benefits_by_tier(segments: list[str], tier_names: list[str]) -> dict[str, list[str]]:
    """Split promotion_content segments into per-tier benefit lines."""
    tiers: dict[str, list[str]] = {name: [] for name in tier_names}
    current: str | None = None
    for raw in segments:
        text = str(raw or "").strip()
        if not text:
            continue
        if text in tier_names:
            current = text
            continue
        if current:
            tiers[current].append(text)
    return tiers


def run(*, client: SupabaseRestClient, apply: bool) -> dict:
    plans = client.fetch_rows(
        TABLE_CLINIC_MEMBERSHIPS,
        "plan_id,membership_name,benefits,source_url",
        filters={"business_id": f"eq.{BUSINESS_ID}"},
        limit=20,
    )
    promos = client.fetch_rows(
        TABLE_CLINIC_PROMOTIONS,
        "promotion_id,source_url,promotion_content",
        filters={"business_id": f"eq.{BUSINESS_ID}"},
        limit=5,
    )
    promo = next((p for p in promos if p.get("promotion_content")), promos[0] if promos else None)
    if not promo:
        raise RuntimeError("no clinic_promotions row for business_id=1181")
    tier_names = [str(p["membership_name"]) for p in plans]
    split = benefits_by_tier(list(promo.get("promotion_content") or []), tier_names)
    now = datetime.now(timezone.utc).isoformat()
    updates: list[dict] = []
    for plan in plans:
        name = str(plan["membership_name"])
        benefits = split.get(name) or []
        updates.append(
            {
                "plan_id": plan["plan_id"],
                "membership_name": name,
                "old_benefits_count": len(plan.get("benefits") or []),
                "new_benefits_count": len(benefits),
                "old_source_url": plan.get("source_url"),
                "new_source_url": SOURCE_URL,
                "benefits_preview": benefits[:3],
            }
        )
        if apply:
            client.update_row(
                TABLE_CLINIC_MEMBERSHIPS,
                {"plan_id": f"eq.{plan['plan_id']}"},
                {
                    "benefits": benefits,
                    "source_url": SOURCE_URL,
                    "updated_at": now,
                },
            )
    audit = {
        "business_id": BUSINESS_ID,
        "promotion_id": promo.get("promotion_id"),
        "source_url": SOURCE_URL,
        "apply": apply,
        "updates": updates,
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    client = SupabaseRestClient(os.getenv("SUPABASE_URL", "").strip(), get_supabase_secret_key())
    audit = run(client=client, apply=args.apply)
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
