"""Tests for clinic_memberships insert row shaping."""
from utils.membership_plans import _benefits_list, build_membership_plan_insert_row


def test_benefits_list_normalizes_json_string():
    assert _benefits_list('["10% off"]') == ["10% off"]


def test_build_membership_plan_insert_row_sets_benefits_and_source_url():
    row = build_membership_plan_insert_row(
        {
            "plan_name": "VIP",
            "monthly_fee": 99,
            "billing_period": "monthly",
            "benefits": ["10% off Botox"],
        },
        {"business_id": 1, "subpage_url": "https://example.com/membership/"},
    )
    assert row["benefits"] == ["10% off Botox"]
    assert row["source_url"] == "https://example.com/membership"
    assert row["membership_price"] == 99
