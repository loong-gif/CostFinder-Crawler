"""Route list-price offers to clinic_services; promos need any discount field."""
import pytest

from scripts.run_domain_architecture_pipeline import schema_offer_to_master
from utils.clinic_service_extraction import (
    is_promo_offer,
    offer_to_clinic_service_item,
)


def test_list_price_offer_not_promo():
    offer = {
        "regular_price": 13,
        "discount_price": None,
        "discount_percent": None,
        "discount_amount": None,
        "offer_raw_text": "0-49 units $13 per unit",
        "items": [{"service_name": "Dysport", "unit_type": "unit"}],
    }
    assert not is_promo_offer(offer)
    item = offer_to_clinic_service_item(offer)
    assert item is not None
    assert item["service_name"] == "Dysport"
    assert item["regular_price"] == 13


def test_promo_offer_with_discount_price():
    offer = {
        "regular_price": 13,
        "discount_price": 11,
        "discount_percent": None,
        "discount_amount": None,
        "offer_raw_text": "First-time new patient special $11 per unit",
        "items": [{"service_name": "Jeuveau", "unit_type": "unit"}],
    }
    assert is_promo_offer(offer)
    master, _items = schema_offer_to_master(
        offer,
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/services",
        membership_plan_id=None,
    )
    assert master["discount_price"] == 11
    assert master["regular_price"] == 13


def test_promo_offer_with_discount_percent_only():
    offer = {
        "regular_price": 5000,
        "discount_price": None,
        "discount_percent": 20,
        "discount_amount": None,
        "offer_raw_text": "Custom Full Facial Balancing 20% off $5,000+",
        "items": [{"service_name": "Others"}],
    }
    assert is_promo_offer(offer)
    master, _items = schema_offer_to_master(
        offer,
        business_id=1,
        promotion_id=2,
        source_url="https://example.com/services",
        membership_plan_id=None,
    )
    assert master["discount_percent"] == 20
    assert "discount_price" not in master


def test_schema_offer_to_master_rejects_list_price_only():
    with pytest.raises(ValueError, match="discount_price, discount_percent, or discount_amount"):
        schema_offer_to_master(
            {
                "regular_price": 800,
                "discount_price": None,
                "discount_percent": None,
                "discount_amount": None,
                "offer_raw_text": "$800 per syringe",
                "items": [{"service_name": "Dermal Filler"}],
            },
            business_id=1,
            promotion_id=2,
            source_url="https://example.com/services",
            membership_plan_id=None,
        )
