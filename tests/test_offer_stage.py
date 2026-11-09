from __future__ import annotations

import json

import pytest
from offer_stage_support import ENUM, ENUM_ROWS, NOW, FakeLLM, existing_item, existing_offer, llm_offer, promo_row

from utils import offer_stage, pipeline_db
from utils.offer_schema import base_offer_schema, offer_schema, service_name_enum, with_service_enum
from utils.offer_stage import build_plan, snapshot_tables
from utils.offer_stage_extract import candidates_from_payload, extract_source
from utils.offer_stage_reconcile import reconcile_items
from utils.offer_stage_spec import MEMBERSHIPS, PROMOTIONS, content_hash, source_evidence


# ---- schema 枚举 ----

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

def test_gift_only_offers_are_filtered():
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
    assert [r["reason"] for r in rejected] == ["no_supported_promo_fact", "no_supported_promo_fact"]


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
    assert "is_active and (is_offer_extracted is not true" in text and "plan_id" in text
    assert "pg_catalog.pg_enum" in text and "clinic_service_name_enum" in text
    for name in ("promotions", "memberships", "offers", "items", "services", "service_enum"):
        assert f"'{name}'" in text


def test_snapshot_selects_sources_whose_inactive_offers_were_linked_after_writing():
    promotions = snapshot_tables()["promotions"][2]
    assert promotions.startswith("is_active and (is_offer_extracted is not true or promotion_id in (")
    # 第二轮：未激活的 offer 写入后 item 才全部关联到服务（link-services 更新了 item 的 updated_at）
    assert "not m.is_active" in promotions and "i.service_id is null" in promotions
    assert "m.updated_at < (select max(i.updated_at)" in promotions
    assert "membership_plan_id" in snapshot_tables()["memberships"][2]


def test_snapshot_adds_sources_queued_for_reextraction():
    tables = snapshot_tables(requeued={"promotions": [7], "memberships": [9]})
    assert "or promotion_id = any('{7}'::int2[]::bigint[])" in tables["promotions"][2]
    assert "or plan_id = any('{9}'::int2[]::bigint[])" in tables["memberships"][2]
    pipeline_db.snapshot_query(tables)


def test_snapshot_query_limit_is_applied_in_sql():
    tables = snapshot_tables(limit=5)
    assert "order by promotion_id limit 5" in tables["promotions"][2]
    assert "order by plan_id limit greatest(5 - (select count(*)" in tables["memberships"][2]
