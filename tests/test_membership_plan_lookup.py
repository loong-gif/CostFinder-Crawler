"""Tests for membership plan lookup helpers."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.membership_plan_lookup import plan_display_name, resolve_plan_fields


def test_plan_display_name_prefers_tier():
    assert plan_display_name({"tier_name": "Platinum", "plan_name": "VIP Monthly"}) == "Platinum"


def test_resolve_plan_fields_from_embedded_plan():
    row = resolve_plan_fields(
        {
            "id": 2,
            "discount_price": 11,
            "membership_plan_id": 50,
            "promo_membership_plans": {
                "tier_name": "BABE Club",
                "plan_name": "BABE Club Monthly",
                "monthly_fee": 199,
                "billing_period": "monthly",
            },
        }
    )
    assert row["membership_display_name"] == "BABE Club"
    assert row["membership_plan_fee"] == 199


def test_resolve_plan_fields_from_enriched_view():
    row = resolve_plan_fields(
        {
            "id": 1,
            "discount_price": 11.5,
            "membership_plan_id": 42,
            "plan_tier_name": "TRU SIGNATURE",
            "plan_display_name": "TRU SIGNATURE $199 MONTH",
            "plan_monthly_fee": 199,
            "plan_billing_period": "monthly",
        }
    )
    assert row["membership_display_name"] == "TRU SIGNATURE"
    assert row["membership_billing_period"] == "monthly"
    assert row["membership_plan_fee"] == 199
