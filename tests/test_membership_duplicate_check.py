from __future__ import annotations

from utils.extraction_persist import persist_promo_offer
from utils.membership_duplicate_check import no_material_term_conflict, same_offer_value


def _offer(**overrides):
    offer = {
        "regular_price": None,
        "discount_price": 11,
        "discount_percent": None,
        "discount_amount": None,
        "conditions": [],
        "min_quantity": None,
        "is_membership_required": False,
        "is_new_customer_required": False,
        "offer_raw_text": "Special: Botox $11 per unit",
        "items": [{"service_name": "Botox", "unit_type": "unit"}],
    }
    return {**offer, **overrides}


class FakeClient:
    def __init__(self, membership_offers):
        self.membership_offers = membership_offers
        self.inserted = []
        self.updated = []

    def fetch_rows(self, table, select, **kwargs):
        if table == "clinic_promotions":
            return [{"promotion_id": 84, "business_id": 1, "campaign_end_date": None, "is_active": True}]
        if table == "clinic_services":
            return [{"service_id": 101, "business_id": 1, "service_name": "Botox"}]
        if table == "promo_offer_master":
            return self.membership_offers
        if table == "promo_offer_items":
            return []
        raise AssertionError(f"unexpected database read: {table}")

    def insert_rows(self, table, rows):
        self.inserted.append((table, rows))
        if table == "promo_offer_master":
            return [{"id": 9001}]
        return rows

    def update_row(self, table, filters, payload):
        self.updated.append((table, filters, payload))
        return []


def _membership(**overrides):
    row = {
        "id": 10378,
        "business_id": 1,
        "membership_plan_id": 479,
        "is_active": True,
        "discount_price": 11,
        "discount_percent": None,
        "discount_amount": None,
        "price_model": "per_unit",
        "is_new_customer_required": False,
        "conditions": [],
        "promo_offer_items": [{"item_name": "Botox", "service_id": 999}],
    }
    return {**row, **overrides}


def _persist(client, offer=None):
    offer = offer or _offer()
    return persist_promo_offer(
        client,
        business_id=1,
        promotion_id=84,
        source_url="https://example.com/specials",
        offer=offer,
        evidence=offer["offer_raw_text"],
    )


def test_skips_promotion_when_active_membership_offer_matches_item_and_price() -> None:
    client = FakeClient([_membership()])

    result = _persist(client)

    assert result["accepted"] is False
    assert result["action"] == "skipped"
    assert result["reason"] == "membership_duplicate"
    assert result["membership_offer_id"] == 10378
    assert not any(table == "promo_offer_master" for table, _rows in client.inserted)


def test_keeps_promotion_when_price_differs() -> None:
    client = FakeClient([_membership(discount_price=12)])

    result = _persist(client)

    assert result["accepted"] is True
    assert result["action"] == "inserted"


def test_membership_required_text_does_not_by_itself_skip_promotion() -> None:
    client = FakeClient([_membership()])

    result = _persist(client, _offer(is_membership_required=True, offer_raw_text="VIP Members: Botox $11 per unit"))

    assert result["accepted"] is False
    assert result["reason"] == "membership_duplicate"


def test_matches_normalized_item_name_even_when_service_ids_differ() -> None:
    client = FakeClient([_membership()])

    result = _persist(client, _offer(items=[{"service_name": " botox ", "unit_type": "unit"}]))

    assert result["reason"] == "membership_duplicate"


def test_falls_back_to_clinic_service_name_when_item_name_is_missing() -> None:
    client = FakeClient([_membership(promo_offer_items=[{"item_name": None, "service_id": 101}])])

    result = _persist(client)

    assert result["reason"] == "membership_duplicate"


def test_keeps_promotion_when_material_condition_conflicts() -> None:
    client = FakeClient([_membership(conditions=["Minimum 20 units"])])

    result = _persist(
        client,
        _offer(
            conditions=[{"requirement": "Minimum 10 units", "evidence": "minimum 10 units"}],
            offer_raw_text="Botox special $11 per unit, minimum 10 units",
        ),
    )

    assert result["accepted"] is True


def test_extra_non_null_price_field_is_not_treated_as_the_same_offer() -> None:
    assert not same_offer_value(
        {"discount_price": 11, "discount_percent": None, "discount_amount": 50},
        {"discount_price": 11, "discount_percent": None, "discount_amount": None},
    )


def test_membership_enrollment_terms_do_not_conflict_with_a_short_promotion_line() -> None:
    assert no_material_term_conflict(
        {"is_new_customer_required": False, "price_model": "per_unit", "conditions": []},
        {
            "is_new_customer_required": False,
            "price_model": "per_unit",
            "conditions": ["Minimum commitment: 12 months"],
        },
    )
