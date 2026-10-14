from __future__ import annotations

from utils.extraction_persist import persist_promo_offer
from utils.membership_duplicate_check import (
    find_matching_membership_offer,
    no_material_term_conflict,
    same_offer_value,
)


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
        self.master = None
        self.items = []

    def fetch_rows(self, table, select, **kwargs):
        if table == "clinic_promotions":
            return [{"promotion_id": 84, "business_id": 1, "campaign_end_date": None, "is_active": True}]
        if table == "clinic_services":
            return [{"service_id": 101, "business_id": 1, "service_name": "Botox"}]
        if table == "clinic_memberships":
            return [{"plan_id": 479, "business_id": 1}]
        if table == "promo_offer_master":
            filters = kwargs.get("filters", {})
            if filters.get("id") == "eq.9001":
                return [{**self.master, "promo_offer_items": self.items}] if self.master else []
            # Honor the source-identity filters so promotion lookups never see plan rows.
            wanted = filters.get("membership_plan_id", "not.is.null")
            return [row for row in self.membership_offers
                    if wanted == "not.is.null" or wanted == f"eq.{row['membership_plan_id']}"]
        if table == "promo_offer_items":
            return self.items
        raise AssertionError(f"unexpected database read: {table}")

    def insert_rows(self, table, rows):
        self.inserted.append((table, rows))
        if table == "promo_offer_master":
            self.master = {"id": 9001, **rows[0]}
            return [self.master]
        self.items.extend(rows)
        return rows

    def update_row(self, table, filters, payload):
        self.updated.append((table, filters, payload))
        target = self.master
        for row in self.membership_offers:
            if filters.get("id") == f"eq.{row['id']}":
                target = row
        target.update(payload)
        return [target]


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
        "regular_price": None,
        "is_package": False,
        "is_membership_required": True,
        "offer_raw_text": "VIP Members: Botox $11 per unit",
        "promo_offer_items": [{"item_name": "Botox", "service_id": 999}],
    }
    return {**row, **overrides}


def _persist(client, offer=None, membership_plan_id=None):
    offer = offer or _offer()
    return persist_promo_offer(
        client,
        business_id=1,
        promotion_id=84,
        source_url="https://example.com/specials",
        offer=offer,
        evidence=offer["offer_raw_text"],
        membership_plan_id=membership_plan_id,
    )


def _member_offer(**overrides):
    return _offer(is_membership_required=True, offer_raw_text="VIP Members: Botox $11 per unit", **overrides)


def _match(client, items=None, **overrides):
    master = {"business_id": 1, "membership_plan_id": 479, "is_membership_required": True,
              "is_new_customer_required": False, "is_package": False, "price_model": "per_unit",
              "regular_price": None, "discount_price": 11, "discount_percent": None,
              "discount_amount": None, "conditions": [],
              "offer_raw_text": "VIP Members: Botox $11 per unit", **overrides}
    return find_matching_membership_offer(client, business_id=1, candidate_master=master,
                                          candidate_items=items or [{"item_name": "Botox"}])


def test_matches_membership_offer_only_for_the_same_resolved_plan() -> None:
    client = FakeClient([_membership()])

    assert _match(client)["id"] == 10378
    assert _match(client, membership_plan_id=None) is None
    assert _match(client, is_membership_required=False) is None
    assert _match(client, offer_raw_text="Botox $11 per unit with any membership") is None


def test_keeps_promotion_when_price_differs() -> None:
    client = FakeClient([_membership(discount_price=12)])

    result = _persist(client)

    assert result["accepted"] is True
    assert result["action"] == "inserted"


def test_membership_required_text_does_not_by_itself_skip_promotion() -> None:
    client = FakeClient([_membership()])

    # Without a caller-resolved plan, a price/name coincidence is not identity.
    result = _persist(client, _member_offer())

    assert result["accepted"] is True
    assert result["action"] == "inserted"


def test_matches_normalized_item_name_even_when_service_ids_differ() -> None:
    client = FakeClient([_membership()])

    assert _match(client, items=[{"service_name": " botox ", "service_id": 1}])["id"] == 10378


def test_falls_back_to_clinic_service_name_when_item_name_is_missing() -> None:
    client = FakeClient([_membership(promo_offer_items=[{"item_name": None, "service_id": 101}])])

    assert _match(client)["id"] == 10378


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


def test_membership_enrollment_terms_omitted_by_a_short_line_are_not_equivalent() -> None:
    # Dedupe requires identical terms; a shorter line is kept for review instead.
    assert not no_material_term_conflict(
        {"is_new_customer_required": False, "price_model": "per_unit", "conditions": []},
        {
            "is_new_customer_required": False,
            "price_model": "per_unit",
            "conditions": ["Minimum commitment: 12 months"],
        },
    )
