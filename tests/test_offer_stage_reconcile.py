from __future__ import annotations

from offer_stage_support import CONTENT, existing_item, existing_offer, llm_offer, one_candidate, promo_row

from utils.offer_stage_reconcile import plan_offers, plan_source
from utils.offer_stage_spec import PROMOTIONS


# ---- 身份、ID 保留、停用 ----

def test_unique_fingerprint_match_preserves_offer_item_ids_and_valid_service_id():
    candidates = one_candidate()
    offers = [existing_offer(100, "Botox special $10/unit")]
    items = {100: [existing_item(500, 100, service_id=55)]}
    actions, rejected = plan_offers(candidates, offers, items, {55}, CONTENT)
    assert rejected == []
    assert actions[0]["action"] == "unchanged" and actions[0]["offer_id"] == 100
    assert actions[0]["items"][0]["offer_item_id"] == 500 and actions[0]["items"][0]["service_id"] == 55


def test_invalid_service_id_is_cleared():
    actions, _ = plan_offers(one_candidate(), [existing_offer(100, "Botox special $10/unit")],
                             {100: [existing_item(500, 100, service_id=55)]}, set(), CONTENT)
    item = actions[0]["items"][0]
    assert actions[0]["action"] == "update" and item["service_id"] is None and item["service_id_cleared"] is True


def test_changed_price_updates_same_offer_id():
    candidates = one_candidate(discount_price=10)
    offers = [existing_offer(100, "Botox special $10/unit", discount_price=12)]
    actions, _ = plan_offers(candidates, offers, {100: [existing_item(500, 100)]}, set(), CONTENT)
    assert actions[0]["action"] == "update" and actions[0]["patch"]["discount_price"] == 10


def test_inactive_match_is_reactivated_not_duplicated():
    actions, _ = plan_offers(one_candidate(), [existing_offer(100, "Botox special $10/unit", active=False)],
                             {100: [existing_item(500, 100)]}, set(), CONTENT)
    assert actions[0]["action"] == "update" and actions[0]["reactivate"] is True


def test_offers_gone_from_content_are_deactivated_not_deleted():
    offers = [existing_offer(100, "Botox special $10/unit"), existing_offer(101, "Dysport $8/unit"),
              existing_offer(102, "Old special", active=False)]
    actions, _ = plan_offers(one_candidate(), offers, {}, set(), CONTENT)
    assert [(a["action"], a["offer_id"]) for a in actions] == [("update", 100), ("deactivate", 101)] or \
           [(a["action"], a["offer_id"]) for a in actions] == [("unchanged", 100), ("deactivate", 101)]
    assert all(a["action"] != "delete" for a in actions)


def test_unmatched_offer_still_in_content_is_kept():
    offers = [existing_offer(100, "Botox special $10/unit"), existing_offer(101, "Buy 2 Radiesse, get the 3rd half off")]
    content = CONTENT + "\nBuy 2 Radiesse, get the 3rd half off (save $475)."
    actions, rejected = plan_offers(one_candidate(), offers, {}, set(), content)
    assert all(a.get("offer_id") != 101 for a in actions)
    assert rejected == [{"offer_raw_text": "Buy 2 Radiesse, get the 3rd half off",
                         "reason": "unmatched_offer_still_in_content", "offer_ids": [101]}]


def test_candidate_containing_old_offer_text_updates_same_offer_id():
    offers = [existing_offer(100, "Botox special")]
    actions, rejected = plan_offers(one_candidate(), offers, {100: [existing_item(500, 100)]}, set(), CONTENT)
    assert [(a["action"], a["offer_id"]) for a in actions] == [("update", 100)]
    assert actions[0]["patch"]["offer_raw_text"] == "Botox special $10/unit" and rejected == []


def test_duplicate_old_offers_contained_in_one_candidate_keep_longest_and_deactivate_rest():
    offers = [existing_offer(100, "Botox special"), existing_offer(101, "Botox special $10")]
    actions, rejected = plan_offers(one_candidate(), offers, {}, set(), CONTENT)
    assert ("deactivate", 100) in [(a["action"], a["offer_id"]) for a in actions]
    assert [a["offer_id"] for a in actions if a["action"] in ("update", "unchanged")] == [101]
    assert rejected == []


def total_price_candidate(raw, name, price, percent):
    item = {"item_name": name, "service_name": "Botox", "quantity": None, "unit_price": None,
            "unit_type": None, "service_area": None}
    return one_candidate(raw, price_model="total", discount_price=price, discount_percent=percent, items=[item])


def test_rewritten_offer_with_same_product_name_keeps_offer_id():
    candidates = total_price_candidate("Forehead TOX\n$69.00\n-48%\n~~$135.00~~", "Forehead TOX", 69, 48)
    offers = [existing_offer(100, "Forehead TOX: $79 (regular $135)")]
    actions, rejected = plan_offers(candidates, offers, {}, set(), "Forehead TOX\n$69.00")
    assert [(a["action"], a["offer_id"]) for a in actions] == [("update", 100)] and rejected == []


def test_product_name_shared_by_two_old_offers_inserts_and_deactivates():
    candidates = total_price_candidate("Naked TOX\n$69.90\n-22%", "Naked TOX", 69.9, 22)
    offers = [existing_offer(100, "Naked TOX: Member 10 units $69.90"),
              existing_offer(101, "Naked TOX: Member 20 units $139.80")]
    actions, _ = plan_offers(candidates, offers, {}, set(), "Naked TOX\n$69.90\n-22%")
    assert sorted((a["action"], a.get("offer_id")) for a in actions) == [
        ("deactivate", 100), ("deactivate", 101), ("insert", None)]


def test_same_text_duplicates_keep_the_oldest_active_offer_and_deactivate_the_rest():
    offers = [existing_offer(101, "botox  special $10 / unit"), existing_offer(100, "Botox special $10/unit"),
              existing_offer(99, "Botox special $10/unit", active=False)]
    actions, rejected = plan_offers(one_candidate(), offers, {100: [existing_item(500, 100)]}, set(), CONTENT)
    assert sorted((a["action"], a["offer_id"]) for a in actions) == [("deactivate", 101), ("unchanged", 100)]
    assert next(a for a in actions if a["action"] == "deactivate")["reason"] == "duplicate_of_matched_offer"
    assert rejected == []


def test_same_text_inactive_duplicates_reactivate_the_newest():
    offers = [existing_offer(100, "Botox special $10/unit", active=False),
              existing_offer(101, "Botox special $10/unit", active=False)]
    actions, rejected = plan_offers(one_candidate(), offers, {101: [existing_item(501, 101)]}, set(), CONTENT)
    assert [(a["action"], a["offer_id"], a["reactivate"]) for a in actions] == [("update", 101, True)]
    assert rejected == []


def test_new_candidate_without_match_is_inserted_with_null_service():
    actions, _ = plan_offers(one_candidate(), [], {}, set(), CONTENT)
    assert actions[0]["action"] == "insert" and actions[0]["items"][0]["service_id"] is None


# ---- 来源级计划 ----

def source_plan(offers, items, valid=frozenset(), payload_offers=None, raw="Botox special $10/unit"):
    row = promo_row([raw])
    payload = {"offers": payload_offers if payload_offers is not None else [llm_offer(raw)]}
    return plan_source(PROMOTIONS, row, payload, raw, offers, items, set(valid))


def test_new_unmapped_offer_is_planned_as_insert():
    plan = source_plan([], {})
    assert [o["action"] for o in plan["offers"]] == ["insert"] and "status" not in plan


def test_gift_only_source_plans_no_offers():
    raw = "Free gift with any visit"
    offer = llm_offer(raw, discount_price=None, price_model="total", items=[
        {"item_name": "Gift", "service_name": None, "quantity": None, "unit_price": None, "unit_type": None,
         "service_area": None}])
    plan = source_plan([], {}, payload_offers=[offer], raw=raw)
    assert plan["offers"] == []


def test_empty_extraction_keeps_offers_still_in_content_and_deactivates_the_rest():
    raw = "Botox special $10/unit"
    offers = [existing_offer(100, raw), existing_offer(101, "Dysport $8/unit")]
    plan = source_plan(offers, {100: [], 101: []}, payload_offers=[], raw=raw)
    assert [(o["action"], o["offer_id"]) for o in plan["offers"]] == [("deactivate", 101)]
    assert [r["reason"] for r in plan["rejected"]] == ["unmatched_offer_still_in_content"]


def test_empty_content_deactivates_its_active_offers():
    row = {**promo_row([]), "promotion_content": None}
    plan = plan_source(PROMOTIONS, row, None, "", [existing_offer(100, "Botox special $10/unit"),
                                                     existing_offer(101, "Old", active=False)], {}, set())
    assert [(o["action"], o["offer_id"]) for o in plan["offers"]] == [("deactivate", 100)]
