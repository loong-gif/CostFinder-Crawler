"""Tests for centralized extraction persistence routing."""
from utils.extraction_persist import build_master_from_offer, route_offer


def test_route_offer_list_price_to_service() -> None:
    offer = {
        "regular_price": 13,
        "discount_price": None,
        "discount_percent": None,
        "discount_amount": None,
        "conditions": [],
        "offer_raw_text": "Dysport $13/unit",
        "items": [{"service_name": "Dysport", "unit_type": "unit"}],
    }
    assert route_offer(offer) == "service"


def test_build_master_from_offer_sets_price_model() -> None:
    offer = {
        "regular_price": 13,
        "discount_price": 11,
        "conditions": [],
        "offer_raw_text": "First-time new patient special $11 per unit",
        "items": [{"service_name": "Jeuveau", "unit_type": "unit"}],
    }
    master, items = build_master_from_offer(
        offer,
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/services",
    )
    assert master["discount_price"] == 11
    assert master["conditions"] == []
    assert master["price_model"] == "per_unit"
    assert items[0]["service_name"]


def test_build_master_from_offer_persists_only_requirement_strings() -> None:
    master, _ = build_master_from_offer(
        {
            "discount_price": 11,
            "conditions": ["Minimum 40 units required"],
            "offer_raw_text": "Botox $11 per unit, minimum 40 units",
            "items": [{"service_name": "Botox", "unit_type": "unit"}],
        },
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/services",
    )

    assert master["conditions"] == ["Minimum 40 units required"]


def test_build_master_from_offer_strips_condition_evidence_before_storage() -> None:
    master, _ = build_master_from_offer(
        {
            "discount_price": 11,
            "conditions": [{"requirement": "Minimum 40 units required", "evidence": "40 units minimum"}],
            "offer_raw_text": "Botox $11 per unit, minimum 40 units",
            "items": [{"service_name": "Botox", "unit_type": "unit"}],
        },
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/services",
    )

    assert master["conditions"] == ["Minimum 40 units required"]


def test_build_master_from_offer_recognizes_unit_variants_and_starting_price() -> None:
    per_syringe, _ = build_master_from_offer(
        {
            "regular_price": 800,
            "discount_price": 650,
            "conditions": [],
            "offer_raw_text": "$650 per syringe (regularly $800)",
            "items": [{"service_name": "Filler"}],
        },
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/services",
    )
    starting, _ = build_master_from_offer(
        {
            "discount_price": 450,
            "conditions": [],
            "offer_raw_text": "Starting at $450",
            "items": [{"service_name": "Laser"}],
        },
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/services",
    )
    package, _ = build_master_from_offer(
        {
            "discount_price": 900,
            "conditions": [],
            "offer_raw_text": "2 syringes for $900",
            "items": [{"service_name": "Filler", "quantity": 2}],
        },
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/services",
    )
    assert per_syringe["price_model"] == "per_unit"
    assert starting["price_model"] == "from"
    assert package["price_model"] == "total"
