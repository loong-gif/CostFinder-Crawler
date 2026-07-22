"""Tests for unified extraction quality audit."""
from __future__ import annotations

from utils.extraction_quality_audit import run_full_audit


def test_run_full_audit_flags_membership_commitment_gap() -> None:
    report = run_full_audit(
        services=[],
        memberships=[
            {
                "plan_id": 1,
                "business_id": 10,
                "membership_name": "The Basics",
                "membership_price": 60,
                "minimum_commitment_months": None,
                "benefits": ["**6-month minimum for The Basics.**"],
                "source_url": "https://example.com/membership",
            }
        ],
        promotions=[],
        offers=[],
        offer_items=[],
        businesses=[{"business_id": 10, "name": "Example Spa"}],
        scrape_urls=[],
        search_urls=[],
    )
    types = {issue.issue_type for issue in report.issues}
    assert "missing_commitment_months" in types


def test_run_full_audit_flags_non_promo_in_master() -> None:
    report = run_full_audit(
        services=[],
        memberships=[],
        promotions=[
            {
                "promotion_id": 1,
                "business_id": 10,
                "promotion_title": "Specials",
                "source_url": "https://example.com/specials",
                "promotion_content": ["Botox $10/unit"],
                "is_active": True,
            }
        ],
        offers=[
            {
                "id": 99,
                "business_id": 10,
                "promotion_id": 1,
                "regular_price": 13,
                "discount_price": None,
                "discount_percent": None,
                "discount_amount": None,
                "is_active": True,
                "offer_fingerprint": "abc",
                "offer_raw_text": "Botox $13/unit",
                "clinic_promotions": {
                    "source_url": "https://example.com/specials",
                    "promotion_title": "Specials",
                },
                "promo_offer_items": [{"service_name": "Botox"}],
            }
        ],
        offer_items=[],
        businesses=[{"business_id": 10, "name": "Example Spa"}],
        scrape_urls=["https://example.com/specials"],
        search_urls=[],
    )
    assert any(issue.issue_type == "non_promo_in_master" for issue in report.issues)
