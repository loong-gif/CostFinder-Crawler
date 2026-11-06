from datetime import datetime, timezone

from utils.offer_split import build_plan, render_apply

NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)


def offer(offer_id, raw, **fields):
    base = {"id": offer_id, "business_id": 1, "promotion_id": None, "membership_plan_id": 5, "offer_raw_text": raw,
            "price_model": "per_unit", "regular_price": None, "discount_price": 12, "discount_percent": None,
            "discount_amount": None, "is_membership_required": True, "is_new_customer_required": False,
            "conditions": [], "is_package": False, "updated_at": NOW}
    return {**base, **fields}


def item(item_id, offer_id, name, service_id=None, unit_price=None):
    return {"offer_item_id": item_id, "offer_id": offer_id, "item_name": name, "service_id": service_id,
            "delivered_units": None, "unit_price": unit_price}


def plan_for(offers, items=()):
    return build_plan({"offers": offers, "items": list(items)})


def test_one_price_over_listed_services_becomes_one_offer_per_service():
    plan = plan_for([offer(1, "Botox/Dysport $12/unit")], [item(10, 1, "Botox/Dysport", unit_price=12)])
    split = plan["splits"][0]
    assert [p["offer_raw_text"] for p in split["pieces"]] == ["Botox | Botox/Dysport $12/unit",
                                                              "Dysport | Botox/Dysport $12/unit"]
    assert [p["item_name"] for p in split["pieces"]] == ["Botox", "Dysport"]
    assert [p["unit_price"] for p in split["pieces"]] == [12, 12]
    assert all(p["activate"] is False for p in split["pieces"])  # per_unit 且未关联 service，等 link-services


def test_percent_discount_over_services_is_copied_to_each_and_activated():
    plan = plan_for([offer(2, "10% off Botox, Dysport", price_model="total", discount_price=None, discount_percent=10)])
    assert all(p["activate"] for p in plan["splits"][0]["pieces"])


def test_offers_that_cannot_be_split_safely_are_skipped():
    cases = {
        "Botox $12/unit, Dysport $4/unit": "text_has_other_prices",
        "Skinvive by Juvéderm, get $100 off.": "services_not_listed",
        "$100 off Facial Filler": "services_not_listed",
        "Botox Membership: Xeomin save $2 per unit": "services_not_listed",
        "BBL | 20% savings on Laser Hair Removal, MOXI, BBL, PICO, Morpheus": "already_independent_branch",
        "$11 per unit Botox, Dysport & MOXI": "unrecognized_words_in_list",
    }
    for raw, reason in cases.items():
        plan = plan_for([offer(3, raw, discount_price=11, discount_amount=100)])
        assert plan["splits"] == [] or reason == "", raw
        if plan["skipped"]:
            assert plan["skipped"][0]["reason"] == reason, raw


def test_apply_checks_state_inserts_pieces_and_deactivates_the_original():
    plan = plan_for([offer(1, "Botox/Dysport $12/unit")])
    sql = render_apply(plan)
    assert "stale_plan: some offers changed since planning" in sql
    assert "exception when unique_violation then null;" in sql
    assert "jsonb_to_recordset" in sql and "r.service || ' | ' || offer_raw_text" in sql
    assert '"service":"Botox"' in sql and '"service":"Dysport"' in sql
    assert sql.index("insert into public.promo_offer_master") < sql.index("set is_active = false")
