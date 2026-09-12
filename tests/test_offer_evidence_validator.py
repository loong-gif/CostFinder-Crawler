"""Tests for the evidence gate used before promo offers are persisted."""
from __future__ import annotations

from utils.extraction_persist import persist_promo_offer, route_and_persist_extraction
from utils.offer_evidence_validator import validate_offer_evidence


def _offer(**overrides):
    offer = {
        "regular_price": None,
        "discount_price": 350,
        "discount_percent": None,
        "discount_amount": None,
        "conditions": [],
        "min_quantity": None,
        "is_membership_required": False,
        "is_new_customer_required": False,
        "offer_raw_text": "Xeomin 50 units Regular $450 Special $350",
        "items": [
            {
                "service_name": "Xeomin",
                "quantity": 50,
                "unit_price": 7,
                "unit_type": "unit",
                "service_area": None,
            }
        ],
    }
    return {**offer, **overrides}


def test_rejects_offer_when_raw_text_is_not_in_page_evidence() -> None:
    decision = validate_offer_evidence(
        _offer(),
        "This page only advertises a complimentary consultation.",
    )

    assert decision.accepted is False
    assert decision.reason == "offer_raw_text_not_supported"


def test_rejects_offer_when_page_evidence_is_blank() -> None:
    decision = validate_offer_evidence(_offer(), "")

    assert decision.accepted is False
    assert decision.reason == "missing_source_evidence"


def test_clears_discount_price_when_text_only_supports_discount_amount() -> None:
    raw = "Get $75 off your first JUVÉDERM treatment"
    decision = validate_offer_evidence(
        _offer(
            discount_price=75,
            discount_amount=75,
            offer_raw_text=raw,
            items=[{"service_name": "Juvéderm"}],
        ),
        f"September specials\n{raw}",
    )

    assert decision.accepted is True
    assert decision.offer["discount_price"] is None
    assert decision.offer["discount_amount"] == 75
    assert any(issue.field == "discount_price" for issue in decision.issues)


def test_price_labels_cannot_support_the_other_price_in_the_same_sentence() -> None:
    raw = "Package value $2,466, your price $1,799."
    valid = validate_offer_evidence(
        _offer(
            regular_price=2466,
            discount_price=1799,
            offer_raw_text=raw,
            items=[{"service_name": "Juvéderm"}],
        ),
        raw,
    )
    swapped = validate_offer_evidence(
        _offer(
            regular_price=1799,
            discount_price=2466,
            offer_raw_text=raw,
            items=[{"service_name": "Juvéderm"}],
        ),
        raw,
    )

    assert valid.accepted is True
    assert valid.offer["regular_price"] == 2466
    assert valid.offer["discount_price"] == 1799
    assert swapped.accepted is False
    assert swapped.reason == "no_supported_promo_fact"


def test_clears_conditions_not_stated_in_offer_evidence() -> None:
    raw = "Summer special: Botox $11 per unit"
    decision = validate_offer_evidence(
        _offer(
            discount_price=11,
            offer_raw_text=raw,
            is_membership_required=True,
            is_new_customer_required=True,
            items=[{"service_name": "Botox", "unit_type": "unit"}],
        ),
        raw,
    )

    assert decision.accepted is True
    assert decision.offer["is_membership_required"] is False
    assert decision.offer["is_new_customer_required"] is False


def test_preserves_explicit_package_quantity_and_exact_derived_unit_price() -> None:
    raw = "Xeomin 50 units Regular $450 Special $350"
    decision = validate_offer_evidence(_offer(regular_price=450), raw)

    assert decision.accepted is True
    assert decision.offer["regular_price"] == 450
    assert decision.offer["discount_price"] == 350
    assert decision.offer["items"][0]["quantity"] == 50
    assert decision.offer["items"][0]["unit_price"] == 7


def test_false_conditions_do_not_require_positive_evidence() -> None:
    raw = "Summer special: Botox $11 per unit"
    decision = validate_offer_evidence(
        _offer(
            discount_price=11,
            offer_raw_text=raw,
            items=[{"service_name": "Botox", "unit_type": "unit"}],
        ),
        raw,
    )

    condition_fields = {"is_membership_required", "is_new_customer_required"}
    assert not any(issue.field in condition_fields for issue in decision.issues)


def test_accepts_conditions_with_source_evidence_and_deduplicates_requirements() -> None:
    raw = "Summer special: Botox $11 per unit. Treat 40 units or more."
    decision = validate_offer_evidence(
        _offer(
            discount_price=11,
            offer_raw_text=raw,
            items=[{"service_name": "Botox", "unit_type": "unit"}],
            conditions=[
                {"requirement": "Treat at least 40 units", "evidence": "Treat 40 units or more"},
                {"requirement": "Treat at least 40 units", "evidence": "40 units or more"},
            ],
        ),
        raw,
    )

    assert decision.accepted is True
    assert decision.offer["conditions"] == ["Treat at least 40 units"]


def test_rejects_conditions_without_source_evidence() -> None:
    decision = validate_offer_evidence(
        _offer(
            conditions=[
                {"requirement": "Treat at least 40 units", "evidence": "minimum 40 units"}
            ]
        ),
        "Xeomin 50 units Regular $450 Special $350",
    )

    assert decision.accepted is False
    assert decision.reason == "invalid_conditions_evidence"
    assert any(issue.field == "conditions[0].evidence" for issue in decision.issues)


def test_rejects_conditions_that_duplicate_boolean_fields() -> None:
    raw = "Member special: Xeomin 50 units Regular $450 Special $350"
    decision = validate_offer_evidence(
        _offer(
            is_membership_required=True,
            offer_raw_text=raw,
            conditions=[
                {"requirement": "Membership required", "evidence": "Member special"}
            ],
        ),
        raw,
    )

    assert decision.accepted is False
    assert decision.reason == "invalid_conditions_evidence"
    assert any(issue.reason == "duplicates_boolean_field" for issue in decision.issues)


def test_rejects_condition_objects_with_extra_fields() -> None:
    raw = "Summer special: Botox $11 per unit"
    decision = validate_offer_evidence(
        _offer(
            offer_raw_text=raw,
            conditions=[{"requirement": "Use within 30 days", "evidence": raw, "source": "page"}],
        ),
        raw,
    )

    assert decision.accepted is False
    assert any(issue.reason == "unexpected_fields" for issue in decision.issues)


def test_rejects_non_string_condition_values() -> None:
    raw = "Summer special: Botox $11 per unit"
    decision = validate_offer_evidence(
        _offer(
            offer_raw_text=raw,
            conditions=[{"requirement": 30, "evidence": raw}],
        ),
        raw,
    )

    assert decision.accepted is False
    assert any(issue.reason == "must_be_string" for issue in decision.issues)


def test_clears_item_values_without_evidence() -> None:
    raw = "Member special: Dysport $4 per unit"
    decision = validate_offer_evidence(
        _offer(
            discount_price=4,
            is_membership_required=True,
            offer_raw_text=raw,
            items=[
                {
                    "service_name": "Dysport",
                    "quantity": 100,
                    "unit_price": 4,
                    "unit_type": "unit",
                    "service_area": "forehead",
                }
            ],
        ),
        raw,
    )

    assert decision.accepted is True
    assert decision.offer["items"][0]["quantity"] is None
    assert decision.offer["items"][0]["unit_price"] == 4
    assert decision.offer["items"][0]["service_area"] is None


def test_persistence_quarantines_before_any_database_access() -> None:
    class NoDatabaseAccess:
        def __getattr__(self, name):
            raise AssertionError(f"database method must not run: {name}")

    result = persist_promo_offer(
        NoDatabaseAccess(),
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/specials",
        offer=_offer(),
        evidence="Unrelated page content",
    )

    assert result["accepted"] is False
    assert result["reason"] == "offer_raw_text_not_supported"
    assert result["action"] == "quarantined"


def test_persistence_quarantines_unmapped_service_items() -> None:
    class FakeClient:
        def fetch_rows(self, table, select, **kwargs):
            if table == "clinic_promotions":
                return [{"promotion_id": 2, "business_id": 1, "campaign_end_date": None}]
            if table == "clinic_services":
                return []
            raise AssertionError(f"unexpected database read: {table}")

    result = persist_promo_offer(
        FakeClient(),
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/specials",
        offer=_offer(
            offer_raw_text="All services 10% off",
            discount_percent=10,
            items=[{"service_name": "All services"}],
        ),
        evidence="All services 10% off",
    )

    assert result["accepted"] is False
    assert result["reason"] == "unmapped_service_item"
    assert result["action"] == "quarantined"


def test_route_stats_count_evidence_rejections_as_quarantined() -> None:
    class NoDatabaseAccess:
        def __getattr__(self, name):
            raise AssertionError(f"database method must not run: {name}")

    stats = route_and_persist_extraction(
        NoDatabaseAccess(),
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/specials",
        offers=[_offer()],
        evidence="Unrelated page content",
    )

    assert stats["quarantined"] == 1
    assert stats["skipped"] == 0
    assert stats["quarantine"][0]["reason"] == "offer_raw_text_not_supported"
