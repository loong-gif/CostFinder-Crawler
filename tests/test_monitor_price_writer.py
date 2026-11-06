"""monitor 计划渲染的 pending SQL：对渲染出的 SQL 文本断言；不连接数据库。"""
from copy import deepcopy
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_monitor_price_sync import snapshot_business_graph
from utils import monitor_price_writer_plan as writer
from utils.monitor_price_writer import render_apply
from utils.monitor_price_plan import build_plan
from utils.promo_offer_items_db import is_blanket_offer_text

TERMS = writer.TERMS


def state():
    return {
        "promotions": [{"promotion_id": 10, "business_id": 7,
            "source_url": "https://www.clinic.example/specials/", "is_active": True,
            "is_offer_extracted": True, "promotion_content": ["Facial $100"],
            "campaign_end_date": None, "updated_at": "2026-10-01T00:00:00+00:00"}],
        "offers": [{"id": 20, "business_id": 7, "promotion_id": 10,
            "membership_plan_id": None, "discount_price": 100, "regular_price": None,
            "price_model": "total", "is_package": False, "conditions": [],
            "offer_raw_text": "Facial $100", "is_active": True, "updated_at": "2026-10-01T00:00:00+00:00"}],
        "items": [{"offer_item_id": 30, "offer_id": 20, "service_id": 40,
            "item_name": "Facial", "delivered_units": 1, "unit_price": 100, "updated_at": "2026-10-01T00:00:00+00:00"}],
        "terms": [],
        "services": [{"service_id": 40, "business_id": 7, "service_name": "Facial",
            "service_area": None, "source_url": "https://clinic.example/facial"}],
    }


SOURCE = {"business_id": 7, "url": "https://clinic.example/specials", "promotion_id": 10}


def plan_for(data=None, operations=None):
    data = data or state()
    before = writer.source_graph(snapshot_business_graph(data, 7), SOURCE)
    return {"schema_version": 1, "status": "ready", "source": dict(SOURCE), "before": before,
            "operations": operations or [
                {"table": "promo_offer_master", "action": "update", "id": 20, "values": {"discount_price": 80}},
                {"table": "promo_offer_items", "action": "update", "id": 30, "values": {"unit_price": 80}}],
            "evidence_summary": {"complete": True}, "review": []}


def terms_plan(*, existing=True, change_conditions=False, new_offer=False):
    """冻结一个真实规划器结果，包括仅 Terms 的修复。"""
    data = state()
    raw = "Facial 1 session Regular $125 Special $100. One per patient"
    master = data["offers"][0]
    master.update(offer_raw_text=raw, regular_price=125, discount_amount=None, discount_percent=None,
                  conditions=["One per patient"], is_membership_required=False, is_new_customer_required=False)
    data["promotions"][0]["promotion_content"] = [raw]
    if existing:
        terms = dict.fromkeys(writer.TABLES[TERMS][2])
        terms.update(offer_id=20, raw_terms=['old, "quote" \\ slash'], is_one_per_patient=False,
                     is_new_customer_required=False, is_membership_required=False,
                     created_at="2026-10-01T00:00:00+00:00", updated_at="2026-10-01T00:00:00+00:00")
        data["terms"] = [terms]
    if change_conditions:
        master["conditions"] = ["Cannot be combined"]
    offer = {k: deepcopy(master[k]) for k in (
        "offer_raw_text", "regular_price", "discount_price", "discount_percent", "discount_amount",
        "price_model", "is_package", "is_membership_required", "is_new_customer_required")}
    offer.update(conditions=["One per patient"], visibility="visible_match", quantity_kind="fixed",
                 items=[{"item_name": "Facial", "service_id": 40, "delivered_units": 1, "unit_price": 100}])
    evidence = {"url": SOURCE["url"], "accessible": True, "complete": True, "markdown": raw, "offers": [offer]}
    if new_offer:
        data["offers"], data["items"], data["terms"] = [], [], []
    graph = writer.source_graph(snapshot_business_graph(data, 7), SOURCE)
    plan = build_plan(dict(SOURCE), graph, evidence, {})
    assert plan["status"] == "ready", plan
    return plan


def sql_of(plan, protections=None):
    return render_apply(plan, protections or {})


def index(sql, fragment):
    assert fragment in sql, fragment
    return sql.index(fragment)


def insert_plan():
    plan = plan_for()
    plan["operations"] = [
        {"table": "clinic_promotions", "action": "insert", "id": -1,
         "values": {"business_id": 7, "source_url": SOURCE["url"], "is_active": True}},
        {"table": "promo_offer_master", "action": "insert", "id": -1,
         "values": {"business_id": 7, "promotion_id": -1, "membership_plan_id": None,
                    "discount_price": 50, "is_package": False, "is_active": True, "offer_raw_text": "Facial $50"}},
        {"table": "promo_offer_items", "action": "insert", "id": -1,
         "values": {"offer_id": -1, "service_id": 40, "item_name": "Facial", "delivered_units": 1, "unit_price": 50}},
    ]
    return plan


def test_single_atomic_do_block_with_plan_digest_header():
    sql = sql_of(plan_for())
    assert sql.count("do $pipeline$") == 1 and sql.rstrip().endswith("$pipeline$;")
    assert re.search(r"^-- plan sha256 [0-9a-f]{64}$", sql, re.M)
    assert not re.search(r"\b(commit|rollback|savepoint)\b", sql, re.I)
    assert "update public.promo_offer_master set discount_price = 80 where id = 20;" in sql
    assert "update public.promo_offer_items set unit_price = 80 where offer_item_id = 30;" in sql
    assert "set constraints all immediate;" in sql
    assert not re.search(r"\b(create|alter|drop)\b", sql, re.I)


def test_rendering_is_deterministic_and_ignores_protection_file_timestamp_only():
    plan = plan_for()
    assert sql_of(plan, {"updated_at": "one"}) == sql_of(plan, {"updated_at": "two"})


def test_precondition_checks_precede_writes_and_postconditions_follow_them():
    sql = sql_of(plan_for())
    first_write = index(sql, "update public.promo_offer_master set discount_price")
    assert index(sql, "stale_before: promotions membership") < first_write
    assert index(sql, "stale_before: promo_offer_master 20") < first_write
    assert index(sql, "requested_fields_changed: promo_offer_master 20") > index(sql, "set constraints all immediate;")


def test_rerun_after_success_fails_on_the_before_snapshot():
    sql = sql_of(plan_for())
    # 同一文件再次执行时，价格已为 80，前置核对里记录的是 100，核对不一致即 raise。
    stale = next(line for line in sql.splitlines() if "stale_before: promo_offer_master 20" in line)
    assert '"discount_price": 100' in stale and "'v1'" not in stale


def test_stale_membership_checks_cover_every_graph_level_by_normalized_source_key():
    sql = sql_of(plan_for())
    for label in ("promotions", "offers", "items", "services"):
        line = next(l for l in sql.splitlines() if f"stale_before: {label} membership" in l)
        assert "is distinct from" in line
    promotions = next(l for l in sql.splitlines() if "stale_before: promotions membership" in l)
    # 别名 URL（http、www、末尾斜杠）按规范化键匹配，不按字符串相等
    assert "regexp_match" in promotions and "'clinic.example/specials'" in promotions and "array[10]" in promotions
    assert "array[20]" in next(l for l in sql.splitlines() if "stale_before: offers membership" in l)
    assert "array[30]" in next(l for l in sql.splitlines() if "stale_before: items membership" in l)
    assert "array[40]" in next(l for l in sql.splitlines() if "stale_before: services membership" in l)


@pytest.mark.parametrize("table,row_id,column", [
    ("clinic_promotions", 10, "updated_at"), ("promo_offer_master", 20, "updated_at"),
    ("promo_offer_items", 30, "updated_at"), ("clinic_services", 40, "service_name")])
def test_every_before_row_is_compared_by_version_and_content(table, row_id, column):
    sql = sql_of(plan_for())
    line = next(l for l in sql.splitlines() if f"stale_before: {table} {row_id}" in l)
    assert column in line and "jsonb_object_agg" in line
    if column == "updated_at":
        assert "::timestamptz" in line


def test_source_key_sql_matches_normalize_source_url():
    pattern = re.compile(writer.URL_KEY_PATTERN)

    def sql_key(url):
        match = pattern.match(url.strip())
        if not match:
            return None
        host, port, path, query = match.groups()
        host = re.sub(r"^www\.", "", host.lower())
        return host + (":" + port if port and port not in ("80", "443") else "") + path.rstrip("/") + ("?" + query if query else "")

    for url in ("https://www.Clinic.example/specials/", "http://clinic.example:80/specials#banner",
                "https://clinic.example:8443/Specials?offer=1", "https://clinic.example", "HTTP://CLINIC.example/a/b/"):
        assert sql_key(url) == writer.normalize_source_url(url)
    for url in ("https://user:password@clinic.example/specials", "ftp://clinic.example/x", "clinic.example/x"):
        assert sql_key(url) is None


def test_blanket_regex_matches_python_blanket_rule():
    regex = re.compile(writer.BLANKET_REGEX_SQL.replace(r"\y", r"\b"), re.I)
    for text in ("20% off all services", "gift card $80", "20% off entire menu", "Facial $80", "ALL PRODUCTS 10% off",
                 "skincare products", "Selected treatments 20% off", "Referral programme", "Botox $10/unit", None, ""):
        assert bool(regex.search(str(text or ""))) == is_blanket_offer_text(text), text


@pytest.mark.parametrize("fragment", [
    "is distinct from v_protected",
    "unexpected_deactivation: promo_offer_master 20",
    "requested_fields_changed: promo_offer_items 30",
    "unexpected_row_or_trigger_change: promo_offer_items",
])
def test_trigger_side_effects_are_checked_after_all_writes(fragment):
    sql = sql_of(plan_for(), {"offer_ids": [999]})
    assert index(sql, fragment) > index(sql, "set constraints all immediate;")


def test_deactivation_check_uses_requested_active_state():
    plan = plan_for(operations=[{"table": "promo_offer_master", "action": "update", "id": 20,
                                 "values": {"is_active": False}}])
    sql = sql_of(plan)
    assert "(to_jsonb(t) -> 'is_active') = 'false'::jsonb)" in next(
        l for l in sql.splitlines() if "unexpected_deactivation: promo_offer_master 20" in l)
    assert "(to_jsonb(t) -> 'is_active') = 'true'::jsonb)" in next(
        l for l in sql.splitlines() if "unexpected_deactivation: clinic_promotions 10" in l)


def test_ownership_is_checked_for_every_before_row():
    sql = sql_of(plan_for())
    for table, row_id in (("clinic_promotions", 10), ("promo_offer_master", 20), ("promo_offer_items", 30)):
        assert f"ownership_changed: {table} {row_id}" in sql


def test_protection_snapshot_is_taken_before_and_compared_after_the_writes():
    sql = sql_of(plan_for(), {"promotion_ids": [99], "offer_ids": [98], "item_ids": [97]})
    assert index(sql, "v_protected := ") < index(sql, "update public.promo_offer_master")
    assert index(sql, "raise exception 'protected_row_changed'") > index(sql, "set constraints all immediate;")
    for table in ("clinic_promotions", "promo_offer_master", "promo_offer_items"):
        assert f"protected_row_missing: {table}" in sql
    # 受保护父行的子行（含触发器新插入的子行）也在快照内
    assert "promotion_id = any('{99}'::bigint[])" in sql and "offer_id in (select id from public.promo_offer_master" in sql


def test_protected_rows_are_checked_for_live_children_before_writing():
    sql = sql_of(plan_for(), {"promotion_ids": [99]})
    assert "raise exception 'protected_operation'" in sql
    assert index(sql, "raise exception 'protected_operation'") < index(sql, "update public.promo_offer_master")


@pytest.mark.parametrize("protections", [
    {"entries": [{"table": "promo_offer_items", "id": 30}]}, {"item_ids": [30]},
    {"promotion_ids": [10]}, {"offer_ids": [20]}])
def test_direct_and_parent_protection_blocks_child_operations(protections):
    with pytest.raises(writer.WriterError, match="protected_row_or_cascade"):
        sql_of(plan_for(), protections)


def test_parent_protection_blocks_new_child():
    plan = insert_plan()
    plan["operations"] = plan["operations"][1:]
    plan["operations"][0]["values"]["promotion_id"] = 10
    with pytest.raises(writer.WriterError, match="protected_row_or_cascade"):
        sql_of(plan, {"promotion_ids": [10]})


def test_protected_parent_reference_by_new_child_is_also_checked_in_sql():
    plan = insert_plan()
    plan["operations"] = plan["operations"][1:]
    plan["operations"][0]["values"]["promotion_id"] = 10
    plan["operations"][1]["values"]["offer_id"] = -1
    sql = sql_of(plan, {"promotion_ids": [77]})
    assert "10 = any('{77}'::bigint[])" in sql


def test_global_blanket_check_is_the_agents_sql():
    sql = sql_of(plan_for())
    assert f"exists ({writer.BLANKET_CHECK_SQL})" in sql
    assert "raise exception 'blanket_service_mapping'" in sql
    assert "m.offer_raw_text ~* '(all services|any service|all products|all treatments|gift card|skin ?care products)'" in sql
    assert sql.rstrip().splitlines()[-3].endswith("raise exception 'blanket_service_mapping'; end if;")


def test_insert_plan_allocates_ids_with_declared_variables_and_activates_after_children():
    sql = sql_of(insert_plan())
    for name in ("v_promotion_1", "v_offer_1", "v_item_1"):
        assert f"  {name} bigint;" in sql
    assert "lock table public.clinic_promotions in share row exclusive mode;" in sql
    assert index(sql, "lock table public.clinic_promotions") < index(sql, "stale_before: promotions membership")
    assert "v_promotion_1 := (select coalesce(max(promotion_id), 0) + 1 from public.clinic_promotions);" in sql
    assert "pg_get_serial_sequence('public.promo_offer_master', 'id')" in sql
    assert "pg_get_serial_sequence('public.promo_offer_items', 'offer_item_id')" in sql
    assert "raise exception 'sequence_lag'" in sql and "raise exception 'missing_existing_sequence'" in sql
    assert "insert into public.promo_offer_master (id, business_id, promotion_id, membership_plan_id, discount_price, is_package, is_active, offer_raw_text)" in sql
    # 新 Offer 先以 inactive 插入，所有子行写完之后再按计划激活
    assert "values (v_offer_1, 7, v_promotion_1, NULL, 50, false, false, 'Facial $50')" in sql
    assert ("insert into public.promo_offer_items (offer_item_id, offer_id, service_id, item_name, delivered_units, unit_price) "
            "values (v_item_1, v_offer_1, 40, 'Facial', 1, 50);") in sql
    assert index(sql, "update public.promo_offer_master set is_active = true where id = v_offer_1;") > \
        index(sql, "insert into public.promo_offer_items")
    assert not re.search(r"\b(create|alter|drop)\b", sql, re.I)


def test_insert_membership_checks_include_new_rows():
    sql = sql_of(insert_plan())
    assert "array[10, v_promotion_1]" in next(l for l in sql.splitlines() if "source_membership_changed: promotions" in l)
    assert "array[20, v_offer_1]" in next(l for l in sql.splitlines() if "source_membership_changed: offers" in l)
    assert "array[30, v_item_1]" in next(l for l in sql.splitlines() if "source_membership_changed: items" in l)
    assert "cross_business_source" in sql


def test_updates_and_inserts_must_touch_exactly_one_row():
    sql = sql_of(insert_plan())
    # 三个插入加一次激活；每条 DML 之后都核对影响行数
    assert sql.count("raise exception 'write_not_one_row'") == 4
    assert sql_of(plan_for()).count("raise exception 'write_not_one_row'") == 2


@pytest.mark.parametrize("values", [{"unexpected": 1}, {"updated_at": "v3"}, {"id": 20}])
def test_allowlist_rejects_fields_before_rendering(values):
    plan = plan_for()
    plan["operations"][0]["values"] = values
    with pytest.raises(writer.WriterError, match="invalid_fields"):
        sql_of(plan)


def test_url_normalization_preserves_path_case_and_query():
    assert writer.normalize_source_url(" HTTP://www.CLINIC.example/Specials/?offer=1#banner ") == "clinic.example/Specials?offer=1"
    assert writer.normalize_source_url("https://clinic.example/specials?offer=1") != writer.normalize_source_url("https://clinic.example/Specials?offer=1")
    with pytest.raises(writer.WriterError):
        writer.normalize_source_url("https://user:password@clinic.example/specials")


def test_plan_shape_is_validated_before_rendering():
    plan = plan_for()
    plan["status"] = "review"
    with pytest.raises(writer.WriterError, match="plan_not_ready"):
        sql_of(plan)
    plan = plan_for()
    del plan["before"]["services"]
    with pytest.raises(writer.WriterError, match="incomplete_before"):
        sql_of(plan)
    plan = plan_for()
    plan["operations"][0]["table"] = "other_table"
    with pytest.raises(writer.WriterError, match="unknown_table"):
        sql_of(plan)
    plan = plan_for()
    plan["operations"][0]["id"] = 999
    with pytest.raises(writer.WriterError, match="operation_outside_source"):
        sql_of(plan)
    plan = plan_for()
    plan["source"]["promotion_id"] = 11
    with pytest.raises(writer.WriterError, match="source_id_mismatch"):
        sql_of(plan)
    plan = plan_for()
    plan["operations"][0]["values"]["business_id"] = 8
    with pytest.raises(writer.WriterError, match="ownership_changed"):
        sql_of(plan)


def test_final_invariants_are_rejected_in_sql_for_active_offers():
    sql = sql_of(plan_for())
    lines = {c: next(l for l in sql.splitlines() if f"raise exception '{c}'" in l) for c in (
        "invalid_offer_source", "offer_without_items", "invalid_quantity", "package_unit_price", "missing_item_name",
        "blanket_service_mapping", "cross_business_mapping", "unit_total_mismatch", "duplicate_service",
        "inactive_promotion")}
    assert "m.membership_plan_id is not null" in lines["invalid_offer_source"] and "m.business_id is distinct from 7" in lines["invalid_offer_source"]
    # 历史 inactive Offer 的旧缺陷（无子项等）不阻断当前更新
    for category in ("offer_without_items", "invalid_quantity", "package_unit_price", "missing_item_name",
                     "unit_total_mismatch", "duplicate_service", "inactive_promotion"):
        assert "m.is_active" in lines[category], category
    assert "not exists (select 1 from public.promo_offer_items i where i.offer_id = m.id)" in lines["offer_without_items"]
    assert "i.delivered_units is not null and i.delivered_units <= 0" in lines["invalid_quantity"]
    assert "coalesce(m.is_package, false) and i.unit_price is not null" in lines["package_unit_price"]
    assert "i.service_id is null and btrim(coalesce(i.item_name, '')) = ''" in lines["missing_item_name"]
    assert "i.service_id is not null" in lines["blanket_service_mapping"] and "~*" in lines["blanket_service_mapping"]
    assert "s.business_id = 7" in lines["cross_business_mapping"]
    assert ("round(i.unit_price * coalesce(i.delivered_units, 1), 2) <> round(m.discount_price, 2)"
            in lines["unit_total_mismatch"])
    assert "not coalesce(m.is_package, false)" in lines["unit_total_mismatch"] and "not (coalesce(m.offer_raw_text" in lines["unit_total_mismatch"]
    assert "having count(*) > 1" in lines["duplicate_service"] and "lower(btrim(coalesce(i.item_name, '')))" in lines["duplicate_service"]
    assert "not coalesce(p.is_active, false)" in lines["inactive_promotion"] and "current_date" in lines["inactive_promotion"]


def test_blanket_offers_and_concrete_null_service_items_are_not_rejected_by_service_rules():
    sql = sql_of(plan_for())
    # 明示的 blanket 文案不参与单价合计与重复服务核对；blanket 链接具体服务才 raise
    mismatch = next(l for l in sql.splitlines() if "raise exception 'unit_total_mismatch'" in l)
    duplicate = next(l for l in sql.splitlines() if "raise exception 'duplicate_service'" in l)
    assert "not (coalesce(m.offer_raw_text" in mismatch and "not (coalesce(m.offer_raw_text" in duplicate
    missing = next(l for l in sql.splitlines() if "raise exception 'missing_item_name'" in l)
    assert "i.service_id is null and btrim" in missing


def test_is_offer_extracted_live_field_is_writable():
    plan = plan_for()
    plan["operations"].append({"table": "clinic_promotions", "id": 10, "action": "update",
                               "values": {"is_offer_extracted": True}})
    sql = sql_of(plan)
    assert "update public.clinic_promotions set is_offer_extracted = true where promotion_id = 10;" in sql
    assert "(to_jsonb(t) -> 'is_offer_extracted') = 'true'::jsonb" in sql


def test_promotion_content_is_written_and_checked_as_jsonb():
    plan = plan_for()
    quotes = [{"offer_raw_text": "Facial $80", "source_quote": "Facial $80"}]
    plan["operations"].append({"table": "clinic_promotions", "id": 10, "action": "update",
                               "values": {"promotion_content": quotes}})
    sql = sql_of(plan)
    literal = """'[{"offer_raw_text": "Facial $80", "source_quote": "Facial $80"}]'::jsonb"""
    assert f"update public.clinic_promotions set promotion_content = {literal} where promotion_id = 10;" in sql
    assert f"(to_jsonb(t) -> 'promotion_content') = {literal}" in sql


def test_live_derived_discounts_are_checked_against_the_formula():
    plan = plan_for()
    plan["operations"][0]["values"].update(regular_price=120, discount_amount=None, discount_percent=None)
    sql = sql_of(plan)
    line = next(l for l in sql.splitlines() if "requested_fields_changed: promo_offer_master 20" in l)
    assert "t.regular_price::numeric > 0 and t.discount_price::numeric >= 0" in line
    assert "t.discount_amount::numeric is not distinct from round(t.regular_price::numeric - t.discount_price::numeric, 2)" in line
    assert "(t.regular_price::numeric - t.discount_price::numeric) * 100 / t.regular_price::numeric, 2)" in line
    # 公式不适用时回到计划里请求的值
    assert "(to_jsonb(t) -> 'discount_amount') = 'null'::jsonb" in line


def test_new_rows_in_the_source_graph_after_the_writes_are_rejected():
    sql = sql_of(plan_for())
    for group in ("promotions", "offers", "items"):
        assert f"source_membership_changed: {group}" in sql


def test_unrelated_rows_are_version_checked_after_the_writes():
    data = state()
    data["offers"].append(dict(data["offers"][0], id=21))
    data["items"].append(dict(data["items"][0], offer_item_id=31, offer_id=21))
    sql = sql_of(plan_for(data))
    after = sql[index(sql, "set constraints all immediate;"):]
    offers_check = next(l for l in after.splitlines() if "unexpected_row_or_trigger_change: promo_offer_master" in l)
    # 被改的 Offer 20 及其子项不参与比较，兄弟 Offer 21 及其子项必须保持原版本
    assert "(21, '2026-10-01T00:00:00+00:00'::timestamptz)" in offers_check and "(20," not in offers_check
    assert "t.id <> all(array[20]::bigint[])" in offers_check
    items_check = next(l for l in after.splitlines() if "unexpected_row_or_trigger_change: promo_offer_items" in l)
    assert "(31, " in items_check and "(30," not in items_check


def test_terms_plan_from_the_planner_updates_only_terms_and_keeps_activation():
    plan = terms_plan(existing=True)
    assert [(op["table"], op["action"], op["id"]) for op in plan["operations"]] == [(TERMS, "update", 20)]
    sql = sql_of(plan)
    assert "update public.promo_offer_terms set" in sql and "where offer_id = 20;" in sql
    assert "update public.promo_offer_master" not in sql
    assert "stale_before: promo_offer_terms 20" in sql and "stale_before: terms membership" in sql
    assert "raw_terms = E'{" in sql or "raw_terms = '{" in sql


def test_missing_terms_are_inserted_with_conflict_update_for_the_master_trigger_row():
    plan = terms_plan(existing=False)
    op = plan["operations"][0]
    assert (op["table"], op["action"], op["id"]) == (TERMS, "insert", 20)
    sql = sql_of(plan)
    assert "insert into public.promo_offer_terms (raw_terms," in sql and "offer_id) values (" in sql
    assert "on conflict (offer_id) do update set raw_terms = excluded.raw_terms" in sql
    assert "offer_id = excluded.offer_id" not in sql


@pytest.mark.parametrize("existing,new_offer", [(True, False), (False, False), (False, True)])
def test_terms_with_master_trigger_are_written_by_upsert_after_the_offer(existing, new_offer):
    plan = terms_plan(existing=existing, change_conditions=True, new_offer=new_offer)
    sql = sql_of(plan)
    assert "on conflict (offer_id)" in sql or existing
    terms_write = index(sql, "public.promo_offer_terms set") if existing else index(sql, "insert into public.promo_offer_terms")
    assert terms_write > index(sql, "public.promo_offer_master")


@pytest.mark.parametrize("protected", [{"offer_ids": [20]}, {"item_ids": [30]}, {"promotion_ids": [10]}])
def test_terms_only_change_respects_parent_and_child_protection(protected):
    with pytest.raises(writer.WriterError, match="protected_row_or_cascade"):
        sql_of(terms_plan(), protected)


def test_terms_request_and_trigger_changes_are_verified_after_the_write():
    sql = sql_of(terms_plan())
    after = sql[index(sql, "set constraints all immediate;"):]
    assert "requested_fields_changed: promo_offer_terms 20" in after
    # 未请求的 Terms 列（如 promo_code）和未涉及的行不得变化
    assert "unexpected_terms_or_trigger_change: 20" in after


def test_master_change_allows_only_legacy_sync_columns_on_terms():
    data = state()
    terms = dict.fromkeys(writer.TABLES[TERMS][2])
    terms.update(offer_id=20, raw_terms=[], created_at="2026-10-01T00:00:00+00:00", updated_at="2026-10-01T00:00:00+00:00")
    data["terms"] = [terms]
    plan = plan_for(data, [{"table": "promo_offer_master", "action": "update", "id": 20,
                            "values": {"conditions": ["Returning CUSTOMER required"], "is_new_customer_required": None}}])
    sql = sql_of(plan)
    line = next(l for l in sql.splitlines() if "unexpected_terms_or_trigger_change: 20" in l)
    for column in ("min_quantity", "promo_code", "is_one_per_patient", "offer_id"):
        assert f"{column}" in line
    for column in ("raw_terms", "is_new_customer_required", "is_returning_customer_required", "is_membership_required"):
        assert f"{column}" not in line.split("= any(")[1].split("::text[]")[0]


@pytest.mark.parametrize("changes,category", [
    ({"id": 99}, "invalid_terms_reference"),
    ({"values": {"offer_id": 99, "raw_terms": []}}, "invalid_terms_operation"),
    ({"values": {"updated_at": "manual"}}, "invalid_terms_operation"),
    ({"values": {"membership_plan_id": 123}}, "ownership_changed"),
])
def test_invalid_terms_operation_fails_before_rendering(changes, category):
    plan = terms_plan()
    plan["operations"][0].update(changes)
    with pytest.raises(writer.WriterError, match=category):
        sql_of(plan)


def test_null_service_item_requires_an_evidenced_name_in_sql():
    sql = sql_of(plan_for())
    assert "raise exception 'missing_item_name'" in sql


def test_unresolved_foreign_key_is_rejected():
    plan = insert_plan()
    plan["operations"][2]["values"]["offer_id"] = -2
    with pytest.raises(writer.WriterError, match="unresolved_foreign_key"):
        sql_of(plan)
