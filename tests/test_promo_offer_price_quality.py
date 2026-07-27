"""Tests for deterministic promo-offer price auditing and repair planning."""

from utils.promo_offer_price_quality import audit_offer_prices, build_price_repair_plan


def _offer(**overrides):
    row = {
        "id": 10,
        "offer_raw_text": "Regular $13, sale $11 per unit",
        "regular_price": 13,
        "discount_price": 11,
        "discount_amount": 2,
        "discount_percent": 15.38,
        "price_model": "per_unit",
        "updated_at": "2026-07-26T00:00:00+00:00",
        "promo_offer_items": [
            {"offer_item_id": 20, "service_id": 1, "quantity": 50, "unit_price": 11}
        ],
    }
    row.update(overrides)
    return row


def test_audit_reports_per_unit_and_derived_price_mismatches() -> None:
    report = audit_offer_prices(
        [_offer(discount_amount=3, promo_offer_items=[{"offer_item_id": 20, "service_id": 1, "quantity": 50, "unit_price": 5.5}])]
    )

    assert {issue.rule_name for issue in report.issues} == {
        "per_unit模式: discount_price=unit_price",
        "discount_amount一致性",
    }


def test_audit_reports_total_mismatch_only_when_unit_price_is_representable() -> None:
    report = audit_offer_prices(
        [_offer(price_model="total", discount_price=100, regular_price=None, discount_amount=None, discount_percent=None,
                promo_offer_items=[{"offer_item_id": 20, "service_id": 1, "quantity": 20, "unit_price": 10}])]
    )

    assert [issue.rule_name for issue in report.issues] == ["total模式: discount_price=unit_price*qty"]


def test_audit_does_not_require_unit_price_for_an_unlinked_item() -> None:
    report = audit_offer_prices(
        [_offer(price_model="total", discount_price=100, regular_price=None, discount_amount=None, discount_percent=None,
                promo_offer_items=[{"offer_item_id": 20, "service_id": None, "quantity": 20, "unit_price": None}])]
    )

    assert report.issues == []


def test_repair_plan_fills_explicit_percentage_and_keeps_ambiguous_free_benefit_for_review() -> None:
    plan = build_price_repair_plan(
        [
            _offer(id=11, offer_raw_text="20% off Botox", regular_price=None, discount_price=None,
                   discount_amount=None, discount_percent=None, promo_offer_items=[]),
            _offer(id=12, offer_raw_text="includes 20 free Botox units", regular_price=None, discount_price=None,
                   discount_amount=None, discount_percent=None, promo_offer_items=[]),
        ]
    )

    assert plan.master_updates == [{"id": 11, "discount_percent": 20.0, "updated_at": "2026-07-26T00:00:00+00:00"}]
    assert [(case.offer_id, case.rule_name) for case in plan.review_cases] == [
        (12, "价格全为NULL")
    ]


def test_repair_plan_recomputes_derived_fields_and_adds_unit_price_update() -> None:
    plan = build_price_repair_plan(
        [_offer(discount_amount=3, discount_percent=10, promo_offer_items=[{"offer_item_id": 20, "service_id": 1, "quantity": 50, "unit_price": 5.5, "updated_at": "2026-07-26T00:00:00+00:00"}])]
    )

    assert plan.master_updates == [{
        "id": 10,
        "discount_amount": 2.0,
        "discount_percent": 15.38,
        "updated_at": "2026-07-26T00:00:00+00:00",
    }]
    assert plan.item_updates == [{
        "offer_item_id": 20,
        "unit_price": 11.0,
        "updated_at": "2026-07-26T00:00:00+00:00",
    }]


def test_repair_plan_does_not_treat_a_lone_package_price_as_a_comparable_discount_price() -> None:
    plan = build_price_repair_plan(
        [_offer(
            id=1465,
            offer_raw_text="Two FREE vials with the purchase of 8 vials for ONLY $5,199",
            regular_price=519.9,
            discount_price=519.9,
            discount_amount=None,
            discount_percent=None,
            price_model="total",
            promo_offer_items=[],
        )]
    )

    assert plan.master_updates == []
