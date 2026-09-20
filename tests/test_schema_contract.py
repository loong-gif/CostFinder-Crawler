"""Static contract checks for schema helpers."""
from typing import Any

from utils.promo_offer_items_db import upsert_offer_items
from utils.schema_contract import (
    OFFER_ITEM_COLUMNS,
    OFFER_MASTER_COLUMNS,
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


def test_offer_item_name_from_embed() -> None:
    row = {"promo_offer_items": [{"item_name": "Botox"}]}
    assert offer_item_name(row) == "Botox"


def test_offer_item_columns_include_item_name_before_service_id() -> None:
    assert OFFER_ITEM_COLUMNS.index("item_name") < OFFER_ITEM_COLUMNS.index("service_id")


def test_offer_master_columns_exclude_removed_moderation_fields() -> None:
    assert "moderation_status" not in OFFER_MASTER_COLUMNS
    assert "moderation_notes" not in OFFER_MASTER_COLUMNS


def test_upsert_offer_items_uses_linked_service_name() -> None:
    class Client:
        def fetch_rows(
            self,
            table: str,
            _select: str,
            **_kwargs: Any,
        ) -> list[dict[str, Any]]:
            if table == TABLE_PROMO_OFFER_ITEMS:
                return []
            return [{"service_id": 7, "service_name": "Botox"}]

        def insert_rows(
            self,
            _table: str,
            rows: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            return rows

    rows = upsert_offer_items(
        Client(), 42, [{"service_id": 7, "quantity": 20, "unit_price": 10}]
    )

    assert rows[0]["item_name"] == "Botox"
    assert rows[0]["service_id"] == 7
