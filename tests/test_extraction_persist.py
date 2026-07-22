"""Tests for centralized extraction persistence routing."""
from utils.extraction_persist import build_master_from_offer, route_offer


def test_route_offer_list_price_to_service() -> None:
    offer = {
        "regular_price": 13,
        "discount_price": None,
        "discount_percent": None,
        "discount_amount": None,
        "offer_raw_text": "Dysport $13/unit",
        "items": [{"service_name": "Dysport", "unit_type": "unit"}],
    }
    assert route_offer(offer) == "service"


def test_build_master_from_offer_sets_price_model() -> None:
    offer = {
        "regular_price": 13,
        "discount_price": 11,
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
    assert master["price_model"] == "per_unit"
    assert items[0]["service_name"]
