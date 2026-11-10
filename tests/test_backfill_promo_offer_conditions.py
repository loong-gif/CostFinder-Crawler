from __future__ import annotations

import json
import sys

import pytest

import scripts.backfill_promo_offer_conditions as backfill
import utils.offer_conditions_backfill_matching as matching
import utils.offer_conditions_backfill_plan as planning
from utils.offer_conditions_backfill_apply import apply_plan
from utils.offer_conditions_backfill_plan import build_plan


class SourceClient:
    def __init__(self, *, missing_source: bool = False) -> None:
        self.calls = []
        self.missing_source = missing_source

    def fetch_rows(self, table, select, **kwargs):
        self.calls.append((table, select, kwargs))
        if table == "promo_offer_master":
            return [
                {
                    "id": 1,
                    "business_id": 10,
                    "promotion_id": 9,
                    "membership_plan_id": None,
                    "offer_raw_text": "Botox $11 per unit, minimum 40 units",
                    "regular_price": None,
                    "discount_price": 11,
                    "discount_percent": None,
                    "discount_amount": None,
                    "is_membership_required": False,
                    "is_new_customer_required": False,
                    "updated_at": "offer-1",
                    "promo_offer_items": [{"item_name": "Botox", "quantity": None, "unit_price": 11}],
                },
                {
                    "id": 2,
                    "business_id": 10,
                    "promotion_id": 9,
                    "membership_plan_id": None,
                    "offer_raw_text": "Lip filler $60 off",
                    "regular_price": None,
                    "discount_price": None,
                    "discount_percent": None,
                    "discount_amount": 60,
                    "is_membership_required": False,
                    "is_new_customer_required": False,
                    "updated_at": "offer-2",
                    "promo_offer_items": [{"item_name": "Dermal Filler", "quantity": None, "unit_price": None}],
                },
                {
                    "id": 3,
                    "business_id": 10,
                    "promotion_id": None,
                    "membership_plan_id": 7,
                    "offer_raw_text": "Filler $500, renewal after 12 months",
                    "regular_price": None,
                    "discount_price": 500,
                    "discount_percent": None,
                    "discount_amount": None,
                    "is_membership_required": True,
                    "is_new_customer_required": False,
                    "updated_at": "offer-3",
                    "promo_offer_items": [{"item_name": "Dermal Filler", "quantity": None, "unit_price": None}],
                },
            ]
        if table == "clinic_promotions":
            return [] if self.missing_source else [{
                "promotion_id": 9,
                "business_id": 10,
                "source_url": "https://example.test/promo",
                "promotion_content": "Botox $11 per unit, minimum 40 units. Lip filler $60 off.",
                "updated_at": "promo-9",
            }]
        if table == "clinic_memberships":
            return [] if self.missing_source else [{
                "plan_id": 7,
                "business_id": 10,
                "source_url": "https://example.test/membership",
                "membership_name": "Gold",
                "minimum_commitment_months": 6,
                "benefits": ["Filler $500, renewal after 12 months."],
                "updated_at": "membership-7",
            }]
        raise AssertionError(table)


def test_build_plan_uses_local_source_matching_and_preserves_empty_conditions() -> None:
    plan = build_plan(SourceClient())

    assert plan["summary"]["source_groups"] == 2
    assert [row["status"] for row in plan["rows"]] == ["ready", "ready", "ready"]
    assert plan["rows"][0]["conditions"] == ["Minimum 40 units required"]
    assert plan["rows"][1]["conditions"] == []
    assert plan["rows"][2]["conditions"] == [
        "after 12 months",
        "Available on the stated anniversary or renewal",
    ]


def test_build_plan_routes_missing_source_to_review() -> None:
    plan = build_plan(SourceClient(missing_source=True))

    assert all(row["status"] == "review" for row in plan["rows"])
    assert all(row["conditions"] is None for row in plan["rows"])
    assert {row["reason"] for row in plan["rows"]} == {"source_not_found"}


class ApplyClient:
    def __init__(self, *, changed_source: bool = False) -> None:
        self.changed_source = changed_source
        self.updates = []

    def fetch_rows(self, table, select, **kwargs):
        del select
        filters = kwargs.get("filters") or {}
        if table == "clinic_promotions":
            return [{"promotion_id": 9, "updated_at": "promo-new" if self.changed_source else "promo-9"}]
        if table == "promo_offer_master":
            return [{"id": 1, "is_active": True, "conditions": None, "updated_at": "offer-1", "promotion_id": 9}]
        raise AssertionError((table, filters))

    def update_row(self, table, filters, payload):
        self.updates.append((table, filters, payload))
        return [{"id": 1, "conditions": payload["conditions"]}]


def test_apply_plan_requires_source_and_offer_preconditions() -> None:
    plan = {
        "plan_version": 1,
        "scope": "active_conditions_null",
        "rows": [
            {
                "offer_id": 1,
                "source_type": "promotion",
                "source_id": 9,
                "source_updated_at": "promo-9",
                "offer_updated_at": "offer-1",
                "conditions": [],
                "status": "ready",
            }
        ],
    }
    changed = ApplyClient(changed_source=True)
    result = apply_plan(plan, changed, changed)
    assert result["applied"] == []
    assert result["conflicts"][0]["reason"] == "source_changed_since_plan"

    client = ApplyClient()
    result = apply_plan(plan, client, client)
    assert result["applied"] == [1]
    assert client.updates[0][1]["conditions"] == "is.null"
    assert client.updates[0][1]["updated_at"] == "eq.offer-1"


def test_apply_plan_rejects_an_unrelated_plan() -> None:
    try:
        apply_plan({"plan_version": 99, "scope": "other", "rows": []}, ApplyClient(), ApplyClient())
    except ValueError as exc:
        assert str(exc) == "unsupported conditions backfill plan"
    else:
        raise AssertionError("expected invalid plan to be rejected")


def test_apply_plan_skips_inactive_and_already_populated_offers() -> None:
    class PreconditionsClient:
        def __init__(self) -> None:
            self.updates = []

        def fetch_rows(self, table, select, **kwargs):
            del select, kwargs
            if table == "clinic_promotions":
                return [{"promotion_id": 9, "updated_at": "promo-9"}]
            if table == "promo_offer_master":
                return [
                    {"id": 1, "is_active": True, "conditions": None, "updated_at": "offer-1"},
                    {"id": 2, "is_active": False, "conditions": None, "updated_at": "offer-2"},
                    {"id": 3, "is_active": True, "conditions": [], "updated_at": "offer-3"},
                ]
            raise AssertionError(table)

        def update_row(self, table, filters, payload):
            self.updates.append((table, filters, payload))
            return [{"id": 1, "conditions": payload["conditions"]}]

    plan = {
        "plan_version": 1,
        "scope": "active_conditions_null",
        "rows": [
            {
                "offer_id": offer_id,
                "source_type": "promotion",
                "source_id": 9,
                "source_updated_at": "promo-9",
                "offer_updated_at": f"offer-{offer_id}",
                "conditions": [],
                "status": "ready",
            }
            for offer_id in (1, 2, 3)
        ],
    }
    client = PreconditionsClient()

    result = apply_plan(plan, client, client)

    assert result["applied"] == [1]
    assert {item["offer_id"] for item in result["conflicts"]} == {2, 3}
    assert [item[1]["id"] for item in client.updates] == ["eq.1"]


def test_source_and_json_helpers_cover_structured_membership_values() -> None:
    assert planning._json_text(None) == ""
    assert planning._json_text(["one", ["two", "three"]]) == "one\ntwo\nthree"
    assert json.loads(planning._json_text({"value": 1})) == {"value": 1}
    assert planning._source_text(
        "membership",
        {"benefits": "Botox $10/unit", "minimum_commitment_months": 6},
    ) == "Botox $10/unit\nMinimum commitment months: 6"
    assert planning._source_key({"promotion_id": 1, "membership_plan_id": 2}) is None


def test_matching_is_conservative_for_ambiguous_or_unlocated_offers() -> None:
    offer = {"offer_raw_text": "Botox $10", "promo_offer_items": [{"item_name": "Botox"}]}
    assert matching._match_source_segments(offer, "") == []
    assert matching._number_hit_count("$10 and 1,200", ["10", "1", "200"]) == 1
    tie_source = "Botox $10. Botox $10. Botox $10."
    assert matching._match_source_segments(offer, tie_source) == []
    assert matching._extract_local_conditions(
        {"offer_raw_text": "Botox $10 minimum 20 units", "promo_offer_items": [{"item_name": "Botox"}]},
        "Botox $10",
    ) == (None, "condition_not_located")


@pytest.mark.parametrize(
    "segment,expected",
    [
        ("Returning patients only", "Returning customer required"),
        ("Minimum 20 units required", "Minimum 20 units required"),
        ("Treatment after 90 days", "after 90 days"),
        ("Available on your anniversary", "Available on the stated anniversary or renewal"),
        ("Please pre-book your visit", "pre-book your visit"),
        ("Consultation required", "Consultation required"),
        ("Activate the manufacturer coupon", "Manufacturer coupon activation or redemption required"),
        ("Not combinable with other offers", "Cannot be combined with other offers"),
        ("While supplies last", "Limited availability"),
        ("Buy 2 treatments, get the next one free", "Purchase requirement: Buy 2 treatments, get the next one free"),
        ("Must be used within 30 days", "Must be used within 30 days"),
    ],
)
def test_condition_candidates_extract_explicit_restrictions(segment: str, expected: str) -> None:
    assert any(item["requirement"].casefold() == expected.casefold() for item in matching._condition_candidates(segment))


def test_resolve_group_marks_empty_source_for_review() -> None:
    offer = {"id": 1, "offer_raw_text": "Botox $10", "updated_at": "offer-1"}
    rows = planning._resolve_group([offer], source_type="promotion", source={"promotion_id": 9})
    assert rows[0]["status"] == "review"
    assert rows[0]["reason"] == "source_content_missing"


def test_build_plan_marks_offers_without_a_single_source_as_review() -> None:
    class OddClient(SourceClient):
        def fetch_rows(self, table, select, **kwargs):
            if table == "promo_offer_master":
                rows = super().fetch_rows(table, select, **kwargs)
                rows.append({"id": 4, "business_id": 10, "promotion_id": 9, "membership_plan_id": 7, "offer_raw_text": "Ambiguous", "updated_at": "offer-4"})
                return rows
            return super().fetch_rows(table, select, **kwargs)

    plan = build_plan(OddClient())
    row = next(row for row in plan["rows"] if row["offer_id"] == 4)
    assert row["status"] == "review"
    assert row["reason"] == "no_condition_source"


def test_fetch_all_handles_a_full_page() -> None:
    class PagedClient:
        def fetch_rows(self, table, select, **kwargs):
            del table, select
            if kwargs["offset"] == 0:
                return [{"id": i} for i in range(planning.PAGE_SIZE)]
            return [{"id": planning.PAGE_SIZE}]

    assert len(planning._fetch_all(PagedClient(), "promo_offer_master", "id")) == planning.PAGE_SIZE + 1


def test_main_writes_a_local_plan_without_model_configuration(monkeypatch, tmp_path) -> None:
    plan = {"plan_version": 1, "scope": "active_conditions_null", "summary": {"targets": 0}, "rows": []}
    monkeypatch.setattr(backfill, "_load_reader", lambda: object())
    monkeypatch.setattr(backfill, "build_plan", lambda client: plan)
    monkeypatch.setattr(sys, "argv", ["backfill", "--output", str(tmp_path / "plan.json")])

    assert backfill.main() == 0
    assert json.loads((tmp_path / "plan.json").read_text()) == plan
