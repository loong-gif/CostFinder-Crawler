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


def test_benefits_by_tier_splits_promotion_segments():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "backfill",
        root / "one-off/20260721_backfill_loulou_memberships.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    segments = [
        "The Works",
        "The Works $295/month",
        "15% off Neurotoxins",
        "The Refresh",
        "The Refresh $150/month",
        "10% off HydraFacials",
    ]
    split = mod.benefits_by_tier(segments, ["The Works", "The Refresh"])
    assert split["The Works"] == ["The Works $295/month", "15% off Neurotoxins"]
    assert split["The Refresh"] == ["The Refresh $150/month", "10% off HydraFacials"]
