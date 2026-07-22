"""Static contract checks for schema helpers."""
from utils.schema_contract import (
    TABLE_CLINIC_MEMBERSHIPS,
    TABLE_CLINIC_PROMOTIONS,
    TABLE_PROMO_OFFER_ITEMS,
    offer_is_active,
    offer_item_name,
)


def test_table_constants() -> None:
    assert TABLE_CLINIC_MEMBERSHIPS == "clinic_memberships"
    assert TABLE_CLINIC_PROMOTIONS == "clinic_promotions"
    assert TABLE_PROMO_OFFER_ITEMS == "promo_offer_items"


def test_offer_is_active_prefers_boolean() -> None:
    assert offer_is_active({"is_active": True}) is True
    assert offer_is_active({"is_active": False}) is False
    assert offer_is_active({"status": "active"}) is True


def test_offer_item_name_from_embed() -> None:
    row = {"promo_offer_items": [{"item_name": "Botox"}]}
    assert offer_item_name(row) == "Botox"
