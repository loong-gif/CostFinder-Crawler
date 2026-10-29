from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from utils import offer_stage, pipeline_db
from utils.offer_schema import base_offer_schema, offer_schema, service_name_enum, with_service_enum
from utils.offer_stage import (
    BLANKET_CHECK_SQL, MEMBERSHIPS, PROMOTIONS, build_plan, candidates_from_payload, content_hash,
    extract_source, fully_mapped, plan_offers, plan_source, reconcile_items, render_apply, snapshot_tables,
    source_evidence,
)

ENUM = ["Botox", "Dysport", "Dermal Filler", "Others"]
NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def llm_offer(raw, **overrides):
    offer = {"purchase_type": "single", "is_package": False, "price_model": "per_unit", "regular_price": None,
             "discount_price": 10, "discount_percent": None, "discount_amount": None, "min_quantity": None,
             "is_membership_required": False, "is_new_customer_required": False, "conditions": [],
             "offer_raw_text": raw,
             "items": [{"item_name": "Botox", "service_name": "Botox", "quantity": None, "unit_price": 10,
                        "unit_type": "unit", "service_area": None}]}
    offer.update(overrides)
    return offer


def promo_row(lines, promotion_id=7):
    return {"promotion_id": promotion_id, "business_id": 3, "source_url": "https://a.test/specials",
            "promotion_title": "Specials", "promotion_content": lines, "is_active": True, "is_offer_extracted": False,
            "updated_at": NOW}


def existing_offer(offer_id, raw, *, active=True, promotion_id=7, plan_id=None, **extra):
    row = {"id": offer_id, "business_id": 3, "promotion_id": promotion_id, "membership_plan_id": plan_id,
           "is_active": active, "updated_at": NOW, "is_new_customer_required": False,
           "is_membership_required": False, "conditions": [], "offer_raw_text": raw, "price_model": "per_unit",
           "regular_price": None, "discount_price": 10, "discount_percent": None, "discount_amount": None,
           "is_package": False}
    row.update(extra)
    return row


def existing_item(item_id, offer_id, name="Botox", service_id=None, units=None, price=10):
    return {"offer_item_id": item_id, "offer_id": offer_id, "item_name": name, "service_id": service_id,
            "delivered_units": units, "unit_price": price}


class FakeLLM:
    model = "fake"

    def __init__(self, offers):
        self.offers, self.calls, self.schemas = offers, 0, []

    def create_json_response(self, messages, *, json_schema=None):
        self.calls += 1
        self.schemas.append(json_schema)
        return {"explanation": "", "offers": self.offers}


# ---- schema 枚举 ----

ENUM_ROWS = [{"enumlabel": name, "enumsortorder": float(i)} for i, name in reversed(list(enumerate(ENUM)))]


def test_json_file_has_no_hardcoded_service_enum():
    item = base_offer_schema()["properties"]["offers"]["items"]["properties"]["items"]["items"]
    assert "enum" not in item["properties"]["service_name"]
    assert item["properties"]["service_name"]["type"] == ["string", "null"]
    assert "item_name" in item["required"]


def test_offer_schema_injects_database_enum_into_copy():
    base = base_offer_schema()
    schema = offer_schema(service_name_enum(ENUM_ROWS))
    service = schema["properties"]["offers"]["items"]["properties"]["items"]["items"]["properties"]["service_name"]
    assert service["enum"] == ENUM + [None]
    assert "enum" not in base["properties"]["offers"]["items"]["properties"]["items"]["items"]["properties"]["service_name"]


def test_service_name_enum_rejects_empty_enum():
    with pytest.raises(RuntimeError, match="empty_enum"):
        service_name_enum([])
    with pytest.raises(ValueError):
        with_service_enum(base_offer_schema(), [])


def test_extract_source_sends_schema_with_runtime_enum_and_caches(tmp_path):
    row = promo_row(["Botox special $10/unit"])
    schema = offer_schema(ENUM)
    client = FakeLLM([llm_offer("Botox special $10/unit")])
    first = extract_source(client, schema, PROMOTIONS, row, source_evidence(PROMOTIONS, row), tmp_path)
    second = extract_source(client, schema, PROMOTIONS, row, source_evidence(PROMOTIONS, row), tmp_path)
    assert first == second and client.calls == 1
    sent = client.schemas[0]["properties"]["offers"]["items"]["properties"]["items"]["items"]["properties"]["service_name"]
    assert sent["enum"] == ENUM + [None]
    changed = promo_row(["Botox $11/unit"])
    extract_source(client, schema, PROMOTIONS, changed, source_evidence(PROMOTIONS, changed), tmp_path)
    assert client.calls == 2 and content_hash(PROMOTIONS, row) != content_hash(PROMOTIONS, changed)


# ---- 抽取与过滤 ----

def test_gift_only_offers_are_filtered_and_do_not_block():
    lines = ["Free gift with any visit", "Buy 3 get 1 free Botox", "Botox special $10/unit"]
    row = promo_row(lines)
    payload = {"offers": [
        llm_offer("Free gift with any visit", discount_price=None, items=[{"item_name": "Gift", "service_name": None,
                  "quantity": None, "unit_price": None, "unit_type": None, "service_area": None}]),
        llm_offer("Buy 3 get 1 free Botox", discount_price=None, price_model="total", items=[
            {"item_name": "Botox", "service_name": "Botox", "quantity": None, "unit_price": None,
             "unit_type": None, "service_area": None}]),
        llm_offer("Botox special $10/unit")]}
    candidates, rejected = candidates_from_payload(payload, PROMOTIONS, row, "\n".join(lines))
    assert [c["master"]["offer_raw_text"] for c in candidates] == ["Botox special $10/unit"]
    assert len(rejected) == 2 and all(not r["blocking"] for r in rejected)
    assert {r["reason"] for r in rejected} <= offer_stage.BENIGN_REASONS


def test_text_not_in_content_is_rejected():
    row = promo_row(["Botox special $10/unit"])
    candidates, rejected = candidates_from_payload({"offers": [llm_offer("Dysport special $9/unit")]}, PROMOTIONS, row,
                                                   "Botox special $10/unit")
    assert candidates == [] and rejected[0]["reason"] == "offer_raw_text_not_supported"


def test_blanket_offer_items_have_no_service():
    raw = "20% off all services"
    row = promo_row([raw])
    offer = llm_offer(raw, discount_price=None, discount_percent=20, price_model="total",
                      items=[{"item_name": "Botox", "service_name": "Botox", "quantity": None, "unit_price": None,
                              "unit_type": None, "service_area": None}])
    candidates, _ = candidates_from_payload({"offers": [offer]}, PROMOTIONS, row, raw)
    assert candidates[0]["blanket"] is True
    assert [i["item_name"] for i in candidates[0]["items"]] == [raw]
    actions = reconcile_items([existing_item(1, 9, name=raw, service_id=55)], candidates[0]["items"],
                              blanket=True, valid_service_ids={55})
    assert actions[0]["action"] == "update" and actions[0]["service_id"] is None


def test_membership_offers_attach_to_plan_and_require_membership():
    raw = "Botox member $9/unit for members"
    row = {"plan_id": 4, "business_id": 3, "source_url": "https://a.test/membership", "membership_name": "Gold",
           "membership_price": 99, "billing_period": "monthly", "benefits": [raw], "is_offer_extracted": False}
    candidates, _ = candidates_from_payload({"offers": [llm_offer(raw, discount_price=9, items=[
        {"item_name": "Botox", "service_name": "Botox", "quantity": None, "unit_price": 9, "unit_type": "unit",
         "service_area": None}])]}, MEMBERSHIPS, row, raw)
    master = candidates[0]["master"]
    assert master["membership_plan_id"] == 4 and master["promotion_id"] is None
    assert master["is_membership_required"] is True


def test_service_name_hint_is_kept_in_plan_not_as_column():
    raw = "Botox special $10/unit"
    candidates, _ = candidates_from_payload({"offers": [llm_offer(raw)]}, PROMOTIONS, promo_row([raw]), raw)
    assert candidates[0]["items"][0]["service_name_hint"] == "Botox"
    actions = reconcile_items([], candidates[0]["items"], blanket=False, valid_service_ids=set())
    assert actions[0]["action"] == "insert" and actions[0]["service_id"] is None


# ---- 身份、ID 保留、停用 ----

def one_candidate(raw="Botox special $10/unit", **kw):
    row = promo_row([raw])
    candidates, _ = candidates_from_payload({"offers": [llm_offer(raw, **kw)]}, PROMOTIONS, row, raw)
    return candidates


CONTENT = "Botox special $10/unit"


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


def test_unmatched_offer_still_in_content_is_kept_and_blocks_completion():
    offers = [existing_offer(100, "Botox special $10/unit"), existing_offer(101, "Buy 2 Radiesse, get the 3rd half off")]
    content = CONTENT + "\nBuy 2 Radiesse, get the 3rd half off (save $475)."
    actions, rejected = plan_offers(one_candidate(), offers, {}, set(), content)
    assert all(a.get("offer_id") != 101 for a in actions)
    assert rejected == [{"offer_raw_text": "Buy 2 Radiesse, get the 3rd half off",
                         "reason": "unmatched_offer_still_in_content", "blocking": True, "offer_ids": [101]}]


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


def test_ambiguous_fingerprint_blocks_candidate_and_keeps_existing_active():
    offers = [existing_offer(100, "Botox special $10/unit"), existing_offer(101, "botox  special $10 / unit")]
    actions, rejected = plan_offers(one_candidate(), offers, {}, set(), CONTENT)
    assert actions == [] and rejected[0]["reason"] == "ambiguous_offer_identity" and rejected[0]["blocking"]


def test_new_candidate_without_match_is_inserted_with_null_service():
    actions, _ = plan_offers(one_candidate(), [], {}, set(), CONTENT)
    assert actions[0]["action"] == "insert" and actions[0]["items"][0]["service_id"] is None


# ---- is_offer_extracted 语义 ----

def source_plan(offers, items, valid=frozenset(), payload_offers=None, raw="Botox special $10/unit"):
    row = promo_row([raw])
    payload = {"offers": payload_offers if payload_offers is not None else [llm_offer(raw)]}
    return plan_source(PROMOTIONS, row, payload, raw, offers, items, set(valid))


def test_new_unmapped_offer_keeps_is_offer_extracted_false():
    plan = source_plan([], {})
    assert plan["status"] == "ready" and plan["set_offer_extracted"] is False


def test_fully_mapped_existing_offer_sets_is_offer_extracted_true():
    plan = source_plan([existing_offer(100, "Botox special $10/unit")], {100: [existing_item(500, 100, service_id=55)]}, {55})
    assert plan["set_offer_extracted"] is True


def test_blanket_only_source_sets_is_offer_extracted_true():
    raw = "20% off all services"
    offer = llm_offer(raw, discount_price=None, discount_percent=20, price_model="total")
    plan = source_plan([], {}, payload_offers=[offer], raw=raw)
    assert plan["set_offer_extracted"] is True and fully_mapped(plan["offers"])


def test_gift_only_source_is_extracted_without_offers():
    raw = "Free gift with any visit"
    offer = llm_offer(raw, discount_price=None, price_model="total", items=[
        {"item_name": "Gift", "service_name": None, "quantity": None, "unit_price": None, "unit_type": None,
         "service_area": None}])
    plan = source_plan([], {}, payload_offers=[offer], raw=raw)
    assert plan["offers"] == [] and plan["set_offer_extracted"] is True


def test_ambiguity_keeps_is_offer_extracted_false():
    offers = [existing_offer(100, "Botox special $10/unit"), existing_offer(101, "botox special $10 unit")]
    items = {100: [existing_item(500, 100, service_id=55)], 101: [existing_item(501, 101, service_id=55)]}
    plan = source_plan(offers, items, {55})
    assert plan["set_offer_extracted"] is False


def test_empty_extraction_with_active_offers_is_blocked():
    plan = source_plan([existing_offer(100, "Botox special $10/unit")], {100: []}, payload_offers=[])
    assert plan["status"] == "blocked" and plan["blocked_reason"] == "empty_extraction_with_active_offers"


# ---- 计划生成 ----

def make_snapshot(promotions=(), memberships=(), offers=(), items=(), services=()):
    return {"promotions": list(promotions), "memberships": list(memberships), "offers": list(offers),
            "items": list(items), "services": list(services), "service_enum": ENUM_ROWS}


def test_build_plan_uses_snapshot_and_calls_llm_once_per_source(tmp_path):
    raw = "Botox special $10/unit"
    snapshot = make_snapshot([promo_row([raw])], offers=[existing_offer(100, raw)],
                             items=[existing_item(500, 100, service_id=55)],
                             services=[{"service_id": 55, "business_id": 3}])
    client = FakeLLM([llm_offer(raw)])
    plan = build_plan(snapshot, client, offer_schema(service_name_enum(snapshot["service_enum"])), tmp_path)
    assert client.calls == 1
    source = plan["sources"][0]
    assert source["status"] == "ready" and source["set_offer_extracted"] is True
    assert source["source_updated_at"] == NOW.isoformat()
    assert source["existing_state"] == {"100": NOW.isoformat()} and source["existing_item_ids"] == {"100": [500]}
    assert source["offers"][0]["action"] == "unchanged" and source["offers"][0]["items"][0]["service_id"] == 55
    json.loads(offer_stage.dump_plan(plan))


def test_build_plan_limit_caps_sources_across_tables(tmp_path):
    member = {"plan_id": 4, "business_id": 3, "source_url": "u", "membership_name": "Gold", "membership_price": 9,
              "billing_period": "monthly", "benefits": [], "is_offer_extracted": False, "updated_at": NOW}
    snapshot = make_snapshot([promo_row([], 8), promo_row([], 7)], [member])
    plan = build_plan(snapshot, FakeLLM([]), offer_schema(ENUM), tmp_path, limit=2)
    assert [(s["table"], s["id"]) for s in plan["sources"]] == [(PROMOTIONS, 7), (PROMOTIONS, 8)]


# ---- 快照查询 ----

def test_snapshot_query_default_selects_unextracted_active_sources():
    text = pipeline_db.snapshot_query(snapshot_tables())
    assert "is_active and is_offer_extracted is not true" in text and "plan_id" in text
    assert "pg_catalog.pg_enum" in text and "clinic_service_name_enum" in text
    for name in ("promotions", "memberships", "offers", "items", "services", "service_enum"):
        assert f"'{name}'" in text


def test_snapshot_query_with_ids_selects_only_given_rows():
    tables = snapshot_tables(promotion_ids=[7])
    assert tables["promotions"][2] == "is_active and promotion_id = any('{7}'::int2[]::bigint[])"
    assert tables["memberships"][2] == "false"
    assert "is_offer_extracted is not true" not in pipeline_db.snapshot_query(tables)


def test_snapshot_query_limit_is_applied_in_sql():
    tables = snapshot_tables(limit=5)
    assert "order by promotion_id limit 5" in tables["promotions"][2]
    assert "order by plan_id limit greatest(5 - (select count(*)" in tables["memberships"][2]


# ---- 应用计划 ----

def ready_plan():
    raw = "Botox special $10/unit"
    offers = [existing_offer(100, raw), existing_offer(101, "Dysport $8/unit")]
    items = {100: [existing_item(500, 100, service_id=55)], 101: [existing_item(501, 101)]}
    plan_row = promo_row([raw, "Dysport special $9/unit"])
    payload = {"offers": [llm_offer(raw), llm_offer("Dysport special $9/unit", items=[
        {"item_name": "Dysport", "service_name": "Dysport", "quantity": None, "unit_price": 9, "unit_type": "unit",
         "service_area": None}], discount_price=9)]}
    source = plan_source(PROMOTIONS, plan_row, payload, source_evidence(PROMOTIONS, plan_row), offers, items, {55})
    return {"sources": [source]}, plan_row, offers, items


def rendered():
    plan, *_ = ready_plan()
    return render_apply(json.loads(offer_stage.dump_plan(plan)))


def test_apply_sql_is_single_do_block_with_freshness_checks_first():
    text = rendered()
    assert text.count("do $pipeline$") == 1 and text.rstrip().endswith("$pipeline$;")
    assert f"'{NOW.isoformat()}'::timestamptz" in text
    first_write = min(text.index(w) for w in ("update public.promo_offer_master", "insert into public.promo_offer_master"))
    for check in ("stale_plan: clinic_promotions 7", "stale_plan: offers of", "stale_plan: items of offer 100", "stale_plan: offer 101 of"):
        assert text.index(check) < first_write
    assert "for update;" in text and "promotion_id = 7" in text
    # 源行与 offer 的 id/updated_at、item id 集合核对
    assert "array_agg(offer_item_id order by offer_item_id)" in text and "'{500}'::int2[]::bigint[]" in text


def test_apply_sql_deactivates_before_insert_then_activates_after_items():
    text = rendered()
    deactivate = text.index("set is_active=false, updated_at=now() where id=101 and is_active")
    insert = text.index("insert into public.promo_offer_master")
    item = text.index("insert into public.promo_offer_items")
    activate = text.index("set is_active=true, updated_at=now() where id=v_offer")
    assert deactivate < insert < item < activate
    assert "false) returning id into v_offer;" in text and "v_new := v_new || v_offer;" in text
    assert "values (v_offer, 'Dysport'" in text
    assert "delete from public.promo_offer_master" not in text


def test_apply_sql_verifies_activation_after_writes():
    text = rendered()
    check = text.index("offer_activation_changed_by_database: clinic_promotions 7")
    assert check > text.index("set is_active=true")
    assert "and is_active) <> cardinality((v_new || '{}'::bigint[]))" in text
    assert "and not is_active) <> 1" in text


def test_apply_sql_runs_blanket_check_on_touched_offers_last():
    text = rendered()
    assert " ".join(BLANKET_CHECK_SQL.split()) in " ".join(text.split())
    assert "and m.id = any(v_touched)" in text and "raise exception 'blanket_check_failed: %', v_violations" in text
    assert text.index("v_touched := v_touched ||") < text.index("blanket_check_failed")


def test_apply_sql_marks_extracted_when_plan_says_so():
    plan, *_ = ready_plan()
    plan["sources"][0]["set_offer_extracted"] = True
    text = render_apply(plan)
    assert "update public.clinic_promotions set is_offer_extracted=true where promotion_id=7;" in text
    assert "is_offer_extracted=true" not in rendered()


def test_apply_sql_skips_blocked_sources():
    plan, *_ = ready_plan()
    plan["sources"][0]["status"] = "blocked"
    text = render_apply(plan)
    assert "insert into public.promo_offer_master" not in text and "stale_plan" not in text
    assert "blanket_check_failed" in text
