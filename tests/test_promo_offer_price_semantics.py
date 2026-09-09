from utils.promo_offer_price_semantics import build_semantic_repair_plan


def _row(**overrides):
    row = {
        "id": 7599,
        "regular_price": None,
        "discount_price": 170,
        "discount_amount": None,
        "discount_percent": None,
        "price_model": "total",
        "offer_raw_text": "Botox – 10 Units | $170",
        "updated_at": "2026-09-09T00:33:28+00:00",
        "promo_offer_items": [
            {
                "offer_item_id": 202,
                "service_id": 758,
                "quantity": 1,
                "unit_price": 170,
                "updated_at": "2026-09-09T01:46:49+00:00",
            }
        ],
    }
    row.update(overrides)
    return row


def test_explicit_quantity_repairs_item_quantity_and_unit_price():
    plan = build_semantic_repair_plan([_row()])

    assert plan.master_updates == []
    assert plan.item_updates == [
        {
            "offer_item_id": 202,
            "quantity": 10,
            "unit_price": 17.0,
            "updated_at": "2026-09-09T01:46:49+00:00",
        }
    ]


def test_total_price_is_restored_when_master_contains_derived_unit_price():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=7768,
                discount_price=7.98,
                offer_raw_text="50 Units Botox! Introductory Offer Only $399!",
                promo_offer_items=[
                    {
                        "offer_item_id": 323,
                        "service_id": 2404,
                        "quantity": 50,
                        "unit_price": 0.1596,
                        "updated_at": "2026-09-09T01:46:49+00:00",
                    }
                ],
            )
        ]
    )

    assert plan.master_updates[0]["discount_price"] == 399.0
    assert plan.master_updates[0].get("price_model", "total") == "total"
    assert plan.item_updates[0]["unit_price"] == 7.98


def test_amount_only_and_value_evidence_do_not_become_transaction_price():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=2958,
                discount_price=75,
                offer_raw_text="GET $75 OFF YOUR FIRST JUVÉDERM® COLLECTION OF FILLERS TREATMENT",
                promo_offer_items=[],
            ),
            _row(
                id=8072,
                discount_price=200,
                offer_raw_text="2 vials of Sculptra & 2 syringes of Hyper Dilute Radiesse ($200 savings)",
                promo_offer_items=[
                    {
                        "offer_item_id": 1769,
                        "service_id": 2294,
                        "quantity": 2,
                        "unit_price": None,
                        "updated_at": "2026-09-09T01:46:49+00:00",
                    }
                ],
            ),
        ]
    )

    by_id = {update["id"]: update for update in plan.master_updates}
    assert by_id[2958]["discount_price"] is None
    assert by_id[2958]["discount_amount"] == 75.0
    assert by_id[8072]["discount_price"] is None
    assert by_id[8072]["discount_amount"] == 200.0


def test_value_and_explicit_unit_price_are_normalized_to_offer_total():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=8122,
                regular_price=None,
                discount_price=179,
                discount_amount=None,
                discount_percent=62.32,
                price_model="per_unit",
                offer_raw_text=(
                    "20 units of BOTOX for just $5.97/unit, plus 10 units free. "
                    "That's 30 units for just $179 ($475 value)."
                ),
                promo_offer_items=[
                    {
                        "offer_item_id": 1796,
                        "service_id": 2243,
                        "quantity": 20,
                        "unit_price": 179,
                        "updated_at": "2026-09-09T01:46:49+00:00",
                    }
                ],
            )
        ]
    )

    master = plan.master_updates[0]
    assert master["regular_price"] == 475.0
    assert master.get("discount_price", 179.0) == 179.0
    assert master["price_model"] == "total"
    assert plan.item_updates[0]["quantity"] == 30
    assert plan.item_updates[0]["unit_price"] is None
    assert any(case["rule_name"] == "item_unit_not_representable_by_live_trigger" for case in plan.review_cases)


def test_multi_item_offer_does_not_allocate_master_total_to_items():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=9000,
                discount_price=750,
                offer_raw_text="2 syringes Skinvive + 25 units Botox = $750",
                promo_offer_items=[
                    {"offer_item_id": 1, "service_id": 10, "quantity": 2, "unit_price": 375, "updated_at": "t1"},
                    {"offer_item_id": 2, "service_id": 11, "quantity": 25, "unit_price": 30, "updated_at": "t2"},
                ],
            )
        ]
    )

    assert {item["unit_price"] for item in plan.item_updates} == {None}


def test_explicit_unit_price_and_quantity_override_a_savings_label():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=8207,
                regular_price=1350,
                discount_price=1200,
                discount_amount=150,
                discount_percent=11.11,
                price_model="per_unit",
                offer_raw_text="Save $150 when you purchase 100 units of Botox at $12/unit - regularly $13.50/unit.",
                promo_offer_items=[],
            )
        ]
    )

    master = plan.master_updates[0]
    assert master["price_model"] == "total"
    assert master.get("discount_price", 1200.0) == 1200.0
    assert master.get("discount_amount", 150.0) == 150.0


def test_value_of_free_add_on_does_not_replace_offer_regular_price():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=4529,
                regular_price=None,
                discount_price=None,
                offer_raw_text=(
                    "One Thermage FLX Treatment Face + Neck Regular Price: $2599 "
                    "Get one Syringe of Juvederm Voluma ($799 Value) – FREE"
                ),
                promo_offer_items=[],
            )
        ]
    )

    master = plan.master_updates[0]
    assert master["regular_price"] == 2599.0


def test_treatment_price_is_a_total_not_an_item_unit_price():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=6670,
                regular_price=1800,
                discount_price=1500,
                discount_amount=300,
                discount_percent=16.67,
                price_model="per_unit",
                offer_raw_text="Others $1500 per treatment",
                promo_offer_items=[
                    {
                        "offer_item_id": 959,
                        "service_id": 1320,
                        "quantity": None,
                        "unit_price": 1500,
                        "updated_at": "2026-09-09T01:46:49+00:00",
                    }
                ],
            )
        ]
    )

    assert plan.master_updates[0]["price_model"] == "total"
    assert plan.item_updates[0]["unit_price"] is None


def test_percent_discount_remains_when_the_other_price_is_only_per_unit():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=1335,
                discount_price=3.60,
                discount_percent=20,
                price_model="per_unit",
                offer_raw_text="TLC Black Card: 20% off Botox / Dysport ($3.60/unit Dysport)",
                promo_offer_items=[],
            )
        ]
    )

    master = plan.master_updates[0]
    assert master["discount_price"] is None
    assert master.get("discount_percent", 20.0) == 20.0
    assert master.get("price_model", "per_unit") == "per_unit"


def test_lone_unit_price_does_not_break_live_discount_constraint():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=1336,
                discount_price=3.70,
                discount_amount=0.30,
                discount_percent=7.5,
                price_model="per_unit",
                offer_raw_text="Dysport $ 3.70 Per Unit",
                promo_offer_items=[],
            )
        ]
    )

    assert plan.master_updates == []
    assert plan.review_cases[0]["rule_name"] == "unit_only_master_not_representable"


def test_clearing_derived_fields_cannot_leave_master_without_discount_field():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=7779,
                regular_price=13,
                discount_price=None,
                discount_amount=2,
                discount_percent=15.38,
                price_model="per_unit",
                offer_raw_text="$11/Unit Botox & Dysport (Reg. $13/Unit)",
                promo_offer_items=[],
            )
        ]
    )

    assert plan.master_updates == []
    assert {case["rule_name"] for case in plan.review_cases} >= {"master_discount_constraint"}


def test_lower_bound_quantity_is_not_multiplied_into_a_total():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=8559,
                regular_price=13,
                discount_price=11,
                discount_amount=2,
                discount_percent=15.38,
                price_model="per_unit",
                offer_raw_text="Botox $11 per unit for 40 units or more; regular price $13 per unit.",
                promo_offer_items=[],
            )
        ]
    )

    assert plan.master_updates == []
    assert plan.review_cases[0]["rule_name"] == "unit_only_master_not_representable"


def test_quantity_and_regular_total_support_service_name_between_number_and_units():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=8580,
                regular_price=520,
                discount_price=420,
                discount_amount=100,
                discount_percent=19.23,
                offer_raw_text="Buy 40 Botox units for $420 ($10.50/unit); regular price $13/unit.",
                promo_offer_items=[
                    {"offer_item_id": 1, "service_id": 1, "quantity": 1, "unit_price": 420, "updated_at": "t"}
                ],
            )
        ]
    )

    assert plan.master_updates == []
    assert plan.item_updates[0]["quantity"] == 40
    assert plan.item_updates[0]["unit_price"] == 10.5


def test_explicit_total_quantity_beats_free_units_and_value_amounts():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=8582,
                discount_price=520,
                offer_raw_text="Buy 40 units of Dysport and get 20 units free ($13/unit: 60 units total).",
                promo_offer_items=[
                    {"offer_item_id": 2, "service_id": 2, "quantity": 40, "unit_price": 13, "updated_at": "t"}
                ],
            )
        ]
    )

    assert plan.master_updates[0]["discount_price"] == 780.0
    assert plan.item_updates[0]["quantity"] == 60
    assert plan.item_updates[0].get("unit_price", 13.0) == 13.0


def test_your_price_is_used_and_multi_item_package_is_not_allocated():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=8604,
                regular_price=2466,
                discount_price=1799,
                discount_amount=667,
                discount_percent=27.05,
                offer_raw_text="Two syringes of JUVÉDERM and 44 units of TOX package, total value $2,466, your price $1,799.",
                promo_offer_items=[
                    {"offer_item_id": 3, "service_id": 3, "quantity": 2, "unit_price": 899.5, "updated_at": "a"},
                    {"offer_item_id": 4, "service_id": 4, "quantity": 44, "unit_price": 40.8864, "updated_at": "b"},
                ],
            )
        ]
    )

    assert plan.master_updates == []
    assert {item["unit_price"] for item in plan.item_updates} == {None}


def test_value_only_is_not_mistaken_for_transaction_price():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=7859,
                discount_price=None,
                offer_raw_text="1 Filler Boost or 1 Sculptra Vial (Up to $1,000 Value)",
                promo_offer_items=[],
            )
        ]
    )

    assert plan.master_updates == []


def test_total_without_explicit_quantity_clears_default_quantity_and_unit():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=6670,
                discount_price=1500,
                offer_raw_text="Others $1500 per treatment",
                promo_offer_items=[
                    {"offer_item_id": 959, "service_id": 1320, "quantity": 1, "unit_price": 1500, "updated_at": "t"}
                ],
            )
        ]
    )

    assert plan.item_updates[0]["quantity"] is None
    assert plan.item_updates[0]["unit_price"] is None


def test_non_terminating_unit_is_left_for_trigger_compatible_review():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=8122,
                discount_price=179,
                offer_raw_text="30 units for just $179 ($475 value)",
                promo_offer_items=[
                    {"offer_item_id": 1796, "service_id": 2243, "quantity": 20, "unit_price": 179, "updated_at": "t"}
                ],
            )
        ]
    )

    assert plan.item_updates[0]["quantity"] == 30
    assert plan.item_updates[0]["unit_price"] is None
    assert any(case["rule_name"] == "item_unit_not_representable_by_live_trigger" for case in plan.review_cases)


def test_last_unlabelled_unit_price_is_the_sale_price_in_a_pair():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=1398,
                discount_price=11,
                price_model="per_unit",
                offer_raw_text="Botox | $13/unit | $11/unit",
                promo_offer_items=[
                    {"offer_item_id": 669, "service_id": 1, "quantity": None, "unit_price": 13, "updated_at": "t"}
                ],
            )
        ]
    )

    assert plan.item_updates[0]["unit_price"] == 11.0


def test_unit_total_does_not_keep_old_regular_or_derived_values():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=7702,
                regular_price=12,
                discount_price=10.45,
                discount_amount=1.55,
                discount_percent=12.92,
                offer_raw_text="Purchase 20 units of Botox $209",
                promo_offer_items=[
                    {"offer_item_id": 538, "service_id": 802, "quantity": 20, "unit_price": 0.5225, "updated_at": "t"}
                ],
            )
        ]
    )

    master = plan.master_updates[0]
    assert master["discount_price"] == 209.0
    assert master["regular_price"] is None
    assert master["discount_amount"] is None
    assert master["discount_percent"] is None


def test_ambiguous_member_price_pair_is_not_rewritten_from_dash_order():
    plan = build_semantic_repair_plan(
        [
            _row(
                id=5367,
                regular_price=1200,
                discount_price=1100,
                discount_amount=100,
                discount_percent=8.33,
                offer_raw_text="Botox Bank (All Neuromodulators: Botox, Dysport, or Xeomin) - $ 1,200 $ 1,100",
                promo_offer_items=[],
            )
        ]
    )

    assert plan.master_updates == []
