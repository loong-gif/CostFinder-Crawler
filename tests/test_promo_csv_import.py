import hashlib

from utils.promo_csv_import import build_import_plan


def _row(**overrides):
    row = {
        "source_url": "https://example.com/specials",
        "source_name": "Example Med Spa",
        "service_name": "Botox",
        "offer_raw_text": "Botox 40 units for $400",
        "is_membership_required": "FALSE",
        "original_price": "500",
        "discount_price": "10",
        "discount_amount": "100",
        "discount_percent": "20",
        "unit_type": "unit",
        "delivered_unit": "40",
        "end_date": "",
    }
    row.update(overrides)
    return row


def test_build_import_plan_uses_existing_promotion_and_per_unit_price():
    plan = build_import_plan(
        [_row()],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert len(plan.accepted) == 1
    item = plan.accepted[0]
    assert item.master["business_id"] == 9
    assert item.master["promotion_id"] == 7
    assert item.master["price_model"] == "per_unit"
    assert item.master["discount_price"] == 10.0
    assert item.master["offer_fingerprint"] == hashlib.sha1(
        b"p:7|botox 40 units for 400"
    ).hexdigest()
    assert item.items == [{"item_name": "Botox", "service_name": "Botox", "unit_price": 10.0}]


def test_build_import_plan_deduplicates_equivalent_source_rows():
    plan = build_import_plan(
        [_row(), _row(offer_raw_text="  Botox  40 units for $400 ")],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert len(plan.accepted) == 1
    assert [rejection.reason for rejection in plan.rejected] == ["duplicate_source_row"]


def test_build_import_plan_rejects_missing_business_mapping_and_non_discount():
    plan = build_import_plan(
        [
            _row(source_url="https://unknown.example/a"),
            _row(source_url="https://example.com/free", discount_price="", discount_amount="", discount_percent=""),
        ],
        promotions_by_url={},
        businesses_by_domain={"example.com": [{"business_id": 9}]},
        today="2026-07-25",
    )

    assert [rejection.reason for rejection in plan.rejected] == [
        "unmapped_business",
        "missing_discount_signal",
    ]


def test_build_import_plan_rejects_per_unit_discount_without_sale_rate():
    plan = build_import_plan(
        [_row(discount_price="", discount_amount="2", discount_percent="10")],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert plan.accepted == []
    assert plan.rejected[0].reason == "per_unit_price_missing"


def test_build_import_plan_uses_unique_exact_business_name_when_domain_is_absent():
    plan = build_import_plan(
        [_row(source_url="https://moved.example/offers", source_name="Example Med Spa")],
        promotions_by_url={},
        businesses_by_domain={},
        businesses_by_name={"example med spa": [{"business_id": 9}]},
        today="2026-07-25",
    )

    assert plan.accepted[0].master["business_id"] == 9
    assert plan.accepted[0].needs_promotion is True


def test_build_import_plan_marks_expired_offer_inactive():
    plan = build_import_plan(
        [_row(end_date="2026-05-15")],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert plan.accepted[0].master["is_active"] is False


def test_build_import_plan_does_not_activate_offer_under_inactive_promotion():
    plan = build_import_plan(
        [_row()],
        promotions_by_url={
            "https://example.com/specials": {
                "promotion_id": 7,
                "business_id": 9,
                "is_active": False,
            }
        },
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert plan.accepted[0].master["is_active"] is False


def test_build_import_plan_splits_package_content_into_items():
    plan = build_import_plan(
        [_row(is_package="TRUE", service_name="Package", offer_content='{"Botox Cosmetic":20,"Juvederm Voluma":1}', unit_type="treatment")],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert plan.accepted[0].items == [
        {"item_name": "Botox Cosmetic", "service_name": "Botox", "quantity": 20},
        {"item_name": "Juvederm Voluma", "service_name": "Juvéderm", "quantity": 1},
    ]


def test_build_import_plan_detects_new_patient_language_and_from_pricing():
    plan = build_import_plan(
        [_row(offer_raw_text="New patients only. Botox starting at $9/unit", discount_price="9")],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert plan.accepted[0].master["is_new_customer_required"] is True
    assert plan.accepted[0].master["price_model"] == "from"


def test_build_import_plan_defaults_only_a_single_total_item_to_one() -> None:
    plan = build_import_plan(
        [_row(unit_type="treatment", delivered_unit="")],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert plan.accepted[0].items == [{"item_name": "Botox", "service_name": "Botox", "quantity": 1}]

    package = build_import_plan(
        [_row(is_package="TRUE", unit_type="treatment", delivered_unit="", offer_content='{"Botox": null}')],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert package.accepted[0].items == [{"item_name": "Botox", "service_name": "Botox"}]


def test_build_import_plan_derives_regular_price_only_from_explicit_savings():
    plan = build_import_plan(
        [_row(original_price="", discount_price="700", discount_amount="100", offer_raw_text="Two syringes $700 (save $100)")],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert plan.accepted[0].master["regular_price"] == 800.0


def test_build_import_plan_copies_per_unit_price_to_each_explicit_item():
    plan = build_import_plan(
        [_row(offer_content='{"Botox": 10, "Dysport": 10}', original_price="", discount_price="13.50", discount_amount="")],
        promotions_by_url={"https://example.com/specials": {"promotion_id": 7, "business_id": 9}},
        businesses_by_domain={},
        today="2026-07-25",
    )

    assert plan.accepted[0].items == [
        {"item_name": "Botox", "service_name": "Botox", "unit_price": 13.5},
        {"item_name": "Dysport", "service_name": "Dysport", "unit_price": 13.5},
    ]
