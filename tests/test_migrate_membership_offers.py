"""Tests for migrating Membership offers to promo_membership_plans."""
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.change_driven_extractor import apply_offer_actions
from utils.membership_plans import (
    build_membership_plan_insert_row_from_offer,
    can_migrate_offer_to_plan,
    find_existing_plan_id,
    infer_tier_name_from_offer,
    offer_row_to_membership_plan,
)


class _FakeClient:
    def __init__(self, plans: Optional[List[Dict[str, Any]]] = None):
        self.plans = plans or []
        self.inserted: List[Dict[str, Any]] = []
        self.updated: List[Dict[str, Any]] = []

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if table != "promo_membership_plans":
            return []
        url = (filters or {}).get("source_url", "").removeprefix("eq.")
        return [row for row in self.plans if row.get("source_url") in {url, url.rstrip("/")}]

    def insert_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.inserted.extend(rows)
        return [{"plan_id": 99}]

    def update_row(self, table: str, row_id_or_filters: Any, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        self.updated.append({"filters": row_id_or_filters, "payload": payload})
        return []


def test_infer_tier_from_raw_service_name():
    offer = {
        "service_name": "Membership",
        "raw_service_name": "Glow Membership",
        "offer_raw_text": "Glow Membership $199/month",
    }
    assert infer_tier_name_from_offer(offer) == "Glow Membership"
    plan = offer_row_to_membership_plan(offer)
    assert plan["tier_name"] == "Glow Membership"
    assert plan["monthly_fee"] == 199
    assert plan["billing_period"] == "monthly"


def test_build_membership_plan_insert_row_from_offer():
    offer = {
        "source_url": "https://example.com/membership",
        "source_name": "example.com",
        "service_name": "Membership",
        "raw_service_name": "VIP",
        "offer_raw_text": "VIP $1200/year",
    }
    row = build_membership_plan_insert_row_from_offer(offer)
    assert row["membership_price"] == 1200
    assert row["billing_period"] == "annual"
    assert row["source_url"] == "https://example.com/membership"
    assert row["benefits"] == []


def test_can_migrate_requires_fee():
    assert can_migrate_offer_to_plan({"service_name": "Membership", "offer_raw_text": "Membership only"}) is False
    assert can_migrate_offer_to_plan({"service_name": "Membership", "offer_raw_text": "$99/month"}) is True


def test_find_existing_plan_id_matches_tier():
    client = _FakeClient(
        plans=[
            {
                "plan_id": 7,
                "plan_name": "Glow Membership $199/month",
                "source_url": "https://example.com/membership",
            }
        ]
    )
    assert find_existing_plan_id(client, "https://example.com/membership/", "Glow Membership") == 7


def test_apply_offer_actions_skips_membership_plan_insert():
    client = _FakeClient()
    result = apply_offer_actions(
        client,
        [
            {
                "action": "insert",
                "service_name": "Membership",
                "offer_raw_text": "Glow Membership $199/month",
            }
        ],
        source_url="https://example.com/membership",
        source_name="example.com",
        dry_run=True,
    )
    assert result["inserted"] == 0
    assert result["skipped"] == 1
