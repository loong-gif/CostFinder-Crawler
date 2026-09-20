from utils.p0_p1_acceptance import acceptance_gate_counts, acceptance_passes


def test_acceptance_gates_count_the_reported_failure_shapes():
    counts = acceptance_gate_counts(
        [
            {
                "id": 1,
                "promotion_id": 9,
                "is_active": True,
                "price_model": "per_unit",
                "discount_price": 13.5,
                "promo_offer_items": [
                    {"service_id": None, "item_name": "Botox", "unit_price": None},
                ],
            }
        ],
        {9: {"is_active": False}},
        {},
    )

    assert counts == {
        "active_offer_on_inactive_promotion": 1,
        "active_item_service_id_null": 1,
        "per_unit_mapped_item_missing_unit_price": 0,
        "item_name_mismatch_service_name": 0,
    }
    assert not acceptance_passes(counts)


def test_acceptance_passes_when_all_hard_gates_are_zero():
    counts = acceptance_gate_counts(
        [
            {
                "id": 1,
                "promotion_id": 9,
                "is_active": True,
                "price_model": "per_unit",
                "discount_price": 13.5,
                "promo_offer_items": [
                    {"service_id": 7, "item_name": "Botox", "unit_price": 13.5},
                ],
            }
        ],
        {9: {"is_active": True}},
        {7: {"service_name": "Botox"}},
    )

    assert acceptance_passes(counts)
