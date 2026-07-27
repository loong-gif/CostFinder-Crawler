"""Tests for promo offer total/unit price repair planning."""

from utils.offer_item_price_repair import build_offer_item_price_plan


def test_sets_single_item_unit_price_from_offer_total() -> None:
    plan = build_offer_item_price_plan(
        [
            {
                "id": 10,
                "discount_price": 350,
                "price_model": "total",
                "promo_offer_items": [
                    {"offer_item_id": 20, "service_id": 1, "quantity": 50, "unit_price": 350}
                ],
            }
        ]
    )

    assert plan.updates == [{"offer_item_id": 20, "unit_price": 7.0}]
    assert plan.skipped == []


def test_preserves_matching_unit_price() -> None:
    plan = build_offer_item_price_plan(
        [
            {
                "id": 10,
                "discount_price": 350,
                "price_model": "total",
                "promo_offer_items": [
                    {"offer_item_id": 20, "service_id": 1, "quantity": 50, "unit_price": 7}
                ],
            }
        ]
    )

    assert plan.updates == []
    assert plan.skipped == []


def test_keeps_per_unit_master_price_and_uses_it_for_each_item() -> None:
    plan = build_offer_item_price_plan(
        [
            {
                "id": 10,
                "regular_price": 9,
                "discount_price": 7,
                "price_model": "per_unit",
                "promo_offer_items": [
                    {"offer_item_id": 20, "service_id": 1, "quantity": 50, "unit_price": 7}
                ],
            }
        ]
    )

    assert plan.master_updates == []
    assert plan.updates == []


def test_skips_starting_at_price() -> None:
    plan = build_offer_item_price_plan(
        [
            {
                "id": 10,
                "discount_price": 7,
                "price_model": "from",
                "promo_offer_items": [{"offer_item_id": 20, "quantity": 50, "unit_price": 7}],
            }
        ]
    )

    assert plan.updates == []
    assert plan.master_updates == []
    assert plan.skipped == [{"offer_id": 10, "reason": "starting_at_price"}]


def test_skips_total_that_cannot_preserve_the_invariant_at_storage_precision() -> None:
    plan = build_offer_item_price_plan(
        [
            {
                "id": 10,
                "discount_price": 100,
                "price_model": "total",
                "promo_offer_items": [{"offer_item_id": 20, "quantity": 3, "unit_price": None}],
            }
        ]
    )

    assert plan.updates == []
    assert plan.skipped == [{"offer_id": 10, "reason": "non_terminating_unit_price"}]


def test_supports_total_that_requires_four_decimal_places() -> None:
    plan = build_offer_item_price_plan(
        [
            {
                "id": 10,
                "discount_price": 624.75,
                "price_model": "total",
                "promo_offer_items": [{"offer_item_id": 20, "quantity": 4, "unit_price": None}],
            }
        ]
    )

    assert plan.updates == [{"offer_item_id": 20, "unit_price": 156.1875}]
    assert plan.skipped == []


def test_normalizes_duplicate_rows_for_the_same_item_using_total_quantity() -> None:
    plan = build_offer_item_price_plan(
        [
            {
                "id": 10,
                "discount_price": 100,
                "price_model": "total",
                "promo_offer_items": [
                    {"offer_item_id": 20, "service_id": 1, "quantity": 4, "unit_price": 25},
                    {"offer_item_id": 21, "service_id": 1, "quantity": 6, "unit_price": 100},
                ],
            }
        ]
    )

    assert plan.updates == [
        {"offer_item_id": 20, "unit_price": 10.0},
        {"offer_item_id": 21, "unit_price": 10.0},
    ]
    assert plan.skipped == []


def test_skips_unknown_price_model_and_item_without_primary_key() -> None:
    plan = build_offer_item_price_plan(
        [
            {"id": 10, "discount_price": 100, "price_model": "", "promo_offer_items": [{"quantity": 2}]},
            {"id": 11, "discount_price": 100, "price_model": "total", "promo_offer_items": [{"quantity": 2}]},
        ]
    )

    assert plan.updates == []
    assert plan.skipped == [
        {"offer_id": 10, "reason": "unsupported_price_model"},
        {"offer_id": 11, "reason": "missing_offer_item_id"},
    ]


def test_clears_unit_price_for_multi_item_package() -> None:
    plan = build_offer_item_price_plan(
        [
            {
                "id": 10,
                "discount_price": 350,
                "promo_offer_items": [
                    {"offer_item_id": 20, "service_id": 1, "quantity": 20, "unit_price": 10},
                    {"offer_item_id": 21, "service_id": 2, "quantity": 1, "unit_price": 150},
                ],
            }
        ]
    )

    assert plan.updates == [
        {"offer_item_id": 20, "unit_price": None},
        {"offer_item_id": 21, "unit_price": None},
    ]
    assert plan.skipped == [{"offer_id": 10, "reason": "multi_item_package"}]


def test_skips_missing_or_invalid_total_or_quantity() -> None:
    plan = build_offer_item_price_plan(
        [
            {"id": 10, "discount_price": None, "price_model": "total", "promo_offer_items": [{"offer_item_id": 20, "quantity": 2}]},
            {"id": 11, "discount_price": 100, "price_model": "total", "promo_offer_items": [{"offer_item_id": 21, "quantity": None}]},
        ]
    )

    assert plan.updates == []
    assert plan.skipped == [
        {"offer_id": 10, "reason": "missing_positive_discount_price"},
        {"offer_id": 11, "reason": "missing_positive_quantity"},
    ]


def test_clears_unit_price_when_the_item_is_not_linked_to_a_service() -> None:
    plan = build_offer_item_price_plan(
        [{
            "id": 10,
            "discount_price": 100,
            "price_model": "total",
            "promo_offer_items": [{"offer_item_id": 20, "service_id": None, "quantity": 10, "unit_price": 10}],
        }]
    )

    assert plan.updates == [{"offer_item_id": 20, "unit_price": None}]
    assert plan.skipped == [{"offer_id": 10, "reason": "unlinked_item"}]
