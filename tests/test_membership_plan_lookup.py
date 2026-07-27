"""Tests for membership plan lookup helpers."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.membership_plan_lookup import plan_display_name, resolve_plan_fields


def test_plan_display_name_uses_current_membership_name():
    assert plan_display_name({"membership_name": "VIP Monthly"}) == "VIP Monthly"


def test_resolve_plan_fields_from_embedded_plan():
    row = resolve_plan_fields(
        {
            "id": 2,
            "discount_price": 11,
            "membership_plan_id": 50,
            "clinic_memberships": {
                "membership_name": "BABE Club Monthly",
                "membership_price": 199,
                "billing_period": "monthly",
            },
        }
    )
    assert row["membership_display_name"] == "BABE Club Monthly"
    assert row["membership_plan_fee"] == 199


def test_resolve_plan_fields_without_membership_relation_is_empty():
    row = resolve_plan_fields(
        {
            "id": 1,
            "discount_price": 11.5,
            "membership_plan_id": 42,
        }
    )
    assert row["membership_display_name"] == ""
    assert row["membership_billing_period"] == ""
    assert row["membership_plan_fee"] is None
