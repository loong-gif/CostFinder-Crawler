"""Tests for Botox offer -> clinic_services backfill mapping."""
from decimal import Decimal

from utils.clinic_services_from_offers import (
    offer_to_clinic_fields,
    pick_winner_botox_offer,
)


def test_pick_winner_prefers_regular_and_unit_over_discount_only():
    offers = [
        {
            "id": 1,
            "service_name": "Botox",
            "regular_price": None,
            "discount_price": 8,
            "unit_type": "unit",
            "is_active": True,
            "is_package": False,
            "offer_raw_text": "promo",
        },
        {
            "id": 2,
            "service_name": "Botox",
            "regular_price": 12,
            "discount_price": 9,
            "unit_type": "unit",
            "is_active": True,
            "is_package": False,
            "offer_raw_text": "menu price $12/unit",
        },
    ]
    winner = pick_winner_botox_offer(offers)
    assert winner is not None
    assert winner["id"] == 2


def test_pick_winner_deprioritizes_package():
    offers = [
        {
            "id": 1,
            "service_name": "Botox",
            "regular_price": 20,
            "discount_price": 15,
            "unit_type": "unit",
            "is_active": True,
            "is_package": True,
            "offer_raw_text": "package",
        },
        {
            "id": 2,
            "service_name": "Botox",
            "regular_price": 11,
            "discount_price": 9,
            "unit_type": "unit",
            "is_active": True,
            "is_package": False,
            "offer_raw_text": "unit menu",
        },
    ]
    winner = pick_winner_botox_offer(offers)
    assert winner["id"] == 2


def test_offer_to_clinic_fields_uses_regular_not_discount():
    fields = offer_to_clinic_fields(
        {
            "service_name": "Botox",
            "regular_price": 12,
            "discount_price": 8,
            "unit_type": "unit",
            "service_area": "Face",
            "service_category": "Neurotoxins",
            "source_url": "https://example.com/services",
        }
    )
    assert fields.regular_price == Decimal("12")
    assert fields.unit_type == "unit"
    assert fields.service_area == "face"


def test_offer_to_clinic_fields_discount_only_leaves_price_null():
    fields = offer_to_clinic_fields(
        {
            "service_name": "Botox",
            "regular_price": None,
            "discount_price": 8,
            "unit_type": "unit",
            "offer_raw_text": "special",
        }
    )
    assert fields.regular_price is None
    assert fields.unit_type == "unit"
