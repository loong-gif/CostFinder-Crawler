"""Delete the exact source records explicitly requested by the operator.

This is intentionally narrow and idempotent: it touches only the listed primary
keys in clinic_promotions and clinic_memberships. Dependent offers are audited
before and after but are not deleted by this script.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from utils.supabase_access import load_supabase_client


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / ".firecrawl" / "delete_explicit_source_records_20260914.json"

PROMOTION_IDS = [
    3570,
    3571,
    3573,
    3575,
    3578,
    3580,
    3582,
    3560,
    3565,
    3566,
    3567,
    3569,
    687,
    108,
    128,
    135,
]
MEMBERSHIP_IDS = [304, 446, 443, 444, 445]


def in_filter(ids: List[int]) -> str:
    return "in.(" + ",".join(str(item) for item in ids) + ")"


def fetch_sources(client: Any) -> Dict[str, List[Dict[str, Any]]]:
    return {
        "clinic_promotions": client.fetch_rows(
            "clinic_promotions",
            "promotion_id,business_id,promotion_title,source_url,is_active,updated_at",
            filters={"promotion_id": in_filter(PROMOTION_IDS)},
            order="promotion_id.asc",
        ),
        "clinic_memberships": client.fetch_rows(
            "clinic_memberships",
            "plan_id,business_id,membership_name,source_url,membership_price,billing_period,updated_at",
            filters={"plan_id": in_filter(MEMBERSHIP_IDS)},
            order="plan_id.asc",
        ),
    }


def fetch_dependent_offers(client: Any) -> Dict[str, List[Dict[str, Any]]]:
    select = "id,business_id,promotion_id,membership_plan_id,is_active,updated_at"
    return {
        "by_promotion": client.fetch_rows(
            "promo_offer_master",
            select,
            filters={"promotion_id": in_filter(PROMOTION_IDS)},
            order="id.asc",
        ),
        "by_membership": client.fetch_rows(
            "promo_offer_master",
            select,
            filters={"membership_plan_id": in_filter(MEMBERSHIP_IDS)},
            order="id.asc",
        ),
    }


def main() -> None:
    client = load_supabase_client(ROOT)
    started_at = datetime.now(timezone.utc).isoformat()
    before = fetch_sources(client)
    dependencies_before = fetch_dependent_offers(client)

    expected = {
        "clinic_promotions": len(PROMOTION_IDS),
        "clinic_memberships": len(MEMBERSHIP_IDS),
    }
    actual = {table: len(rows) for table, rows in before.items()}
    if actual != expected:
        raise RuntimeError(f"Preflight mismatch; refusing to delete: expected={expected}, actual={actual}")

    deleted: Dict[str, List[Dict[str, Any]]] = {}
    deleted["clinic_promotions"] = client.delete_rows(
        "clinic_promotions", {"promotion_id": in_filter(PROMOTION_IDS)}
    )
    deleted["clinic_memberships"] = client.delete_rows(
        "clinic_memberships", {"plan_id": in_filter(MEMBERSHIP_IDS)}
    )

    after = fetch_sources(client)
    dependencies_after = fetch_dependent_offers(client)
    remaining = {table: len(rows) for table, rows in after.items()}
    if remaining != {"clinic_promotions": 0, "clinic_memberships": 0}:
        raise RuntimeError(f"Delete verification failed; remaining={remaining}")

    audit = {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "operation": "hard_delete_exact_source_records",
        "targets": {
            "clinic_promotions": PROMOTION_IDS,
            "clinic_memberships": MEMBERSHIP_IDS,
        },
        "preflight_counts": actual,
        "dependent_offers_before": dependencies_before,
        "deleted_counts": {table: len(rows) for table, rows in deleted.items()},
        "deleted_ids": {
            "clinic_promotions": sorted(row["promotion_id"] for row in deleted["clinic_promotions"]),
            "clinic_memberships": sorted(row["plan_id"] for row in deleted["clinic_memberships"]),
        },
        "remaining_source_counts": remaining,
        "dependent_offers_after": dependencies_after,
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUDIT_PATH.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "deleted_counts": audit["deleted_counts"],
        "deleted_ids": audit["deleted_ids"],
        "remaining_source_counts": remaining,
        "dependent_offer_counts_before": {key: len(value) for key, value in dependencies_before.items()},
        "dependent_offer_counts_after": {key: len(value) for key, value in dependencies_after.items()},
        "audit_path": str(AUDIT_PATH),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
