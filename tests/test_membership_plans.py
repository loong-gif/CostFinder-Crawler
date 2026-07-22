"""Tests for membership plan helpers."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.membership_paths import is_membership_page_url
from utils.membership_plans import (
    build_priced_offer_insert_row,
    membership_offer_fingerprint,
    normalize_membership_plan,
    normalize_membership_payload,
)


def test_is_membership_page_url():
    assert is_membership_page_url("https://example.com/membership")
    assert is_membership_page_url("https://example.com/plans/memberships")
    assert is_membership_page_url("https://example.com/membership-plans")
    assert not is_membership_page_url("https://example.com/specials")


def test_normalize_membership_payload():
    payload = {
        "membership_plans": [
            {
                "tier_name": "VIP",
                "plan_name": "VIP $99/month",
                "monthly_fee": "99",
                "billing_period": "monthly",
                "benefits": ["10% off skincare"],
                "priced_offers": [{"service_name": "botox", "price": 11.5, "unit_type": "unit"}],
            }
        ]
    }
    plans = normalize_membership_payload(payload)
    assert len(plans) == 1
    assert plans[0]["tier_name"] == "VIP"
    assert plans[0]["monthly_fee"] == 99.0
    assert len(plans[0]["priced_offers"]) == 1


def test_build_priced_offer_insert_row():
    row = build_priced_offer_insert_row(
        {"service_name": "Botox", "price": 11.5, "unit_type": "unit", "regular_price": 13.5},
        membership_plan_id=42,
        staging_row={"subpage_url": "https://example.com/membership", "domain_name": "example.com"},
    )
    assert row["membership_plan_id"] == 42
    assert row["discount_price"] == 11.5
    assert row["regular_price"] == 13.5
    assert row["last_verified_at"]
    assert "membership_price" not in row


def test_membership_offer_fingerprint_dedupes():
    fp1 = membership_offer_fingerprint(
        membership_plan_id=1,
        service_name="Botox",
        unit_type="unit",
        discount_price=11.5,
    )
    fp2 = membership_offer_fingerprint(
        membership_plan_id=1,
        service_name="Botox",
        unit_type="unit",
        discount_price=11.5,
    )
    assert fp1 == fp2


def test_normalize_membership_plan_defaults_billing_period():
    plan = normalize_membership_plan({"tier_name": "Gold", "billing_period": "quarterly"})
    assert plan["billing_period"] == "monthly"
