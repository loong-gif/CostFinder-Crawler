"""Tests for utils.promo_offer_audit."""
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.promo_offer_audit import (
    audit_rows,
    build_fingerprint_duplicate_groups,
    is_high_confidence_skincare_product,
)


def _base_row(**overrides):
    row = {
        "id": 1,
        "status": "active",
        "source_name": "example.com",
        "source_url": "https://example.com/pricing",
        "service_name": "Botox",
        "service_category": "Neurotoxins",
        "offer_raw_text": "Botox $11 per unit limited time",
        "regular_price": 15,
        "discount_price": 11,
        "unit_type": "unit",
        "business_id": 10,
        "offer_fingerprint": "abc123",
    }
    row.update(overrides)
    if "is_active" not in overrides:
        row["is_active"] = row.get("status") == "active"
    return row


def test_active_missing_business_id_flagged():
    issues, _, summary = audit_rows([_base_row(business_id=None)], master_business_ids={10})
    types = {issue.issue_type for issue in issues}
    assert "missing_business_id" in types
    assert summary["active_high_severity_count"] >= 1


def test_membership_plan_scope_without_fk():
    issues, _, _ = audit_rows(
        [
            _base_row(
                service_name="Membership",
                offer_raw_text="VIP membership $199 per month",
                membership_plan_id=None,
                status="ended",
            )
        ]
    )
    assert any(issue.issue_type == "scope_membership_plan" for issue in issues)


def test_membership_treatment_price_not_flagged_as_plan():
    issues, _, _ = audit_rows(
        [
            _base_row(
                service_name="Botox",
                membership_plan_id=5,
                offer_raw_text="Member price Botox $9/unit",
            )
        ],
        membership_plan_ids={5},
    )
    assert not any(issue.issue_type == "scope_membership_plan" for issue in issues)


def test_discount_gt_regular_price():
    issues, _, _ = audit_rows([_base_row(regular_price=10, discount_price=12)])
    assert any(issue.issue_type == "discount_price_gt_regular_price" for issue in issues)


def test_active_past_end_date():
    issues, _, _ = audit_rows(
        [_base_row(end_date="2020-01-01")],
        today=date(2026, 7, 15),
    )
    assert any(issue.issue_type == "active_past_end_date" for issue in issues)


def test_fingerprint_duplicate_groups():
    rows = [
        _base_row(id=1, offer_fingerprint="same"),
        _base_row(id=2, offer_fingerprint="same"),
        _base_row(id=3, offer_fingerprint="other", status="ended"),
    ]
    groups = build_fingerprint_duplicate_groups(rows)
    assert len(groups) == 1
    assert groups[0]["count"] == 2


def test_no_offer_content_false_positive():
    issues, _, _ = audit_rows([_base_row()])
    assert not any("offer_content" in issue.issue_type for issue in issues)


def test_membership_source_metadata_is_used_for_required_fields():
    row = _base_row(
        is_active=True,
        promotion_id=None,
        membership_plan_id=5,
        clinic_memberships={
            "plan_id": 5,
            "membership_name": "VIP Membership",
            "source_url": "https://example.com/membership",
        },
    )
    issues, _, _ = audit_rows([row], membership_plan_ids={5})
    issue_types = {issue.issue_type for issue in issues}
    assert "missing_source_name" not in issue_types
    assert "missing_source_url" not in issue_types
    assert "missing_promotion_id" not in issue_types


def test_mapped_service_prevents_consultation_false_positive():
    row = _base_row(
        is_active=True,
        service_name="",
        offer_raw_text="Typically, the cost is $10 per unit, determined during consultation.",
        promo_offer_items=[{"service_id": 96}],
    )
    issues, _, _ = audit_rows(
        [row],
        service_rows={96: {"service_id": 96, "business_id": 10, "service_name": "Botox"}},
    )
    assert not any(issue.issue_type == "scope_consultation" for issue in issues)


def test_high_confidence_skincare():
    assert is_high_confidence_skincare_product(
        {"service_name": "AnteAGE MD Serum", "offer_raw_text": "AnteAGE MD Serum $89"}
    )
    assert not is_high_confidence_skincare_product(
        {"service_name": "TrapTox", "offer_raw_text": "TrapTox $12/unit"}
    )
