"""Tests for extraction_semantic_quality gates."""
from __future__ import annotations

from utils.extraction_semantic_quality import (
    audit_membership_benefits_semantics,
    audit_promotion_content_semantics,
    gate_membership_semantics,
    gate_promotion_semantics,
)
from utils.recent_raw_extraction import validate_membership, validate_promotion


def test_soft_cta_promo_fails_gate() -> None:
    item = {
        "promotion_title": "Resident Specials",
        "promotion_content": [
            "Exclusive pricing for residents of our neighboring communities.",
            "Mention this page when you book your first appointment with us.",
        ],
    }
    issues = audit_promotion_content_semantics(title=item["promotion_title"], segments=item["promotion_content"])
    assert any(i.issue_type == "SOFT_CTA_ONLY" for i in issues)
    assert not gate_promotion_semantics(item).accepted


def test_bound_promo_passes_semantic_gate() -> None:
    item = {
        "promotion_title": "July Specials",
        "promotion_content": ["Botox $11/unit | Regular $14/unit | Save 20% on filler"],
    }
    assert not any(i.severity == "high" for i in audit_promotion_content_semantics(**{
        "title": item["promotion_title"],
        "segments": item["promotion_content"],
    }))
    assert validate_promotion(item, "").accepted


def test_fragmented_hydrafacial_prices_fail() -> None:
    segments = [
        "Hydrafacial Signature",
        "$180",
        "$230",
        "Hydrafacial Deluxe",
        "Hydrafacial Platinum",
    ]
    issues = audit_promotion_content_semantics(title="Monthly specials", segments=segments)
    assert any(i.issue_type == "FRAGMENTED_UNBOUND_PRICES" for i in issues)
    assert not validate_promotion(
        {"promotion_title": "Monthly specials", "promotion_content": segments},
        "",
    ).accepted


def test_shopify_markdown_residue_fails() -> None:
    segments = ["Regular price $99.00", "Sale price $79.00", "Unit price / per"]
    assert any(
        i.issue_type == "MARKDOWN_OR_SHOPIFY_RESIDUE"
        for i in audit_promotion_content_semantics(title="Shop", segments=segments)
    )


def test_membership_discount_only_benefits_fail() -> None:
    item = {"membership_name": "VIP", "membership_price": 150, "benefits": ["10% off skincare"]}
    assert any(
        i.issue_type == "INCOMPLETE_BENEFITS_DISCOUNT_ONLY"
        for i in audit_membership_benefits_semantics(item)
    )
    assert not validate_membership(item, "").accepted


def test_duplicate_benefit_bullet_flagged() -> None:
    item = {
        "membership_name": "Massage Membership",
        "membership_price": 89,
        "benefits": [
            "In Massachusetts, massage is offered at the Back Bay location only.",
            "In Massachusetts, massage is offered at the Back Bay location only.",
            "One 60-minute massage per month.",
        ],
    }
    issues = audit_membership_benefits_semantics(item)
    assert any(i.issue_type == "DUPLICATE_BENEFIT_BULLET" for i in issues)
    assert validate_membership(item, "").accepted


def test_null_benefits_with_price_fails() -> None:
    item = {"membership_name": "Essence", "membership_price": 99, "benefits": []}
    assert not gate_membership_semantics(item).accepted
    assert not validate_membership(item, "").accepted


def test_needs_ocr_empty_benefits_fails() -> None:
    item = {"membership_name": "Club", "membership_price": 99, "benefits": []}
    assert not gate_membership_semantics(item, is_ocr_required=True).accepted
