from copy import deepcopy
import re

import pytest

from utils.data_quality_writer import render_audit, render_business, scope
from utils.pending_sql import list_pending, read_header
from utils.sql_checks import row_matches

STAMP = "2026-10-01T00:00:00+00:00"


def snapshot():
    return {"promotions": [{"promotion_id": 1, "business_id": 7, "source_url": "https://clinic.example/specials",
                            "is_active": True, "updated_at": STAMP}],
            "memberships": [], "services": [], "terms": [],
            "offers": [{"id": 1, "business_id": 7, "promotion_id": 1, "membership_plan_id": None,
                        "is_active": True, "discount_price": None, "regular_price": None, "updated_at": STAMP}],
            "items": [{"offer_item_id": 101, "offer_id": 1, "item_name": "Botox", "service_id": None,
                       "updated_at": STAMP}],
            "confirmations": [], "audit": []}


def repair(data, **changes):
    return {"table": "promo_offer_master", "id": 1, "business_id": 7, "rule": "unsupported_price",
            "source_url": "https://clinic.example/specials", "before": deepcopy(data["offers"][0]),
            "values": {"is_active": False}, **changes}


def render(data, repairs, tmp_path, protections=None):
    return render_audit({"repairs": repairs}, data, tmp_path, protections or {})


def sql_text(result):
    return open(result["rendered"][0]["sql_path"], encoding="utf-8").read()


def test_patch_is_one_atomic_block_with_requested_value_and_unrelated_rows_verified(tmp_path):
    data = snapshot()
    result = render(data, [repair(data)], tmp_path)
    sql = sql_text(result)
    assert result["completed"] and len(result["rendered"]) == 1
    assert sql.count("do $pipeline$") == 1 and not re.search(r"\b(commit|rollback)\b", sql, re.I)
    assert "update public.promo_offer_master set is_active = false where id = 1;" in sql
    after = sql[sql.index("set constraints all immediate;"):]
    assert "requested_fields_changed: promo_offer_master 1" in after
    # 兄弟行与未涉及的表必须保持原版本；被改行及其子项、父行不参与比较
    assert "unexpected_row_or_trigger_change: promo_offer_items" in after
    assert "unexpected_row_or_trigger_change: clinic_services" in after
    assert "unexpected_scope_membership_change: clinic_memberships" in after
    assert "raise exception 'blanket_service_mapping'" in after
    memberships = next(l for l in after.splitlines() if "unexpected_row_or_trigger_change: clinic_promotions" in l)
    assert "t.promotion_id <> all(array[1]::bigint[])" in memberships


def test_scope_and_target_are_checked_before_any_update_so_a_change_aborts_without_writes(tmp_path):
    data = snapshot()
    sql = sql_text(render(data, [repair(data)], tmp_path))
    update = sql.index("update public.promo_offer_master")
    for label in ("clinic_promotions", "clinic_memberships", "clinic_services", "promo_offer_master",
                  "promo_offer_items", "promo_offer_terms"):
        assert sql.index(f"stale_plan: scope_changed {label}") < update
    line = next(l for l in sql.splitlines() if "stale_plan: target_changed promo_offer_master 1" in l)
    # 目标行的每个列都参与核对
    assert "jsonb_object_agg" in line and "discount_price" in line and "updated_at" in line
    # 子项被人工修改（updated_at 变化）时，范围核对按 (id, updated_at) 发现
    items = next(l for l in sql.splitlines() if "stale_plan: scope_changed promo_offer_items" in l)
    assert "(101, '2026-10-01T00:00:00+00:00'::timestamptz)" in items


def test_parent_protection_blocks_child_deactivation_and_renders_nothing(tmp_path):
    data = snapshot()
    result = render(data, [repair(data)], tmp_path, {"promotion_ids": [1]})
    assert result["rendered"] == [] and result["skipped"][0]["reason"] == "manual_protection"
    assert result["completed"] is False
    assert not (tmp_path / "pending_sql").exists()


def test_admin_audit_does_not_create_protection(tmp_path):
    data = snapshot()
    data["audit"] = [{"id": 10, "target_type": "deal", "target_id": "1", "action": "update", "created_at": "now"}]
    result = render(data, [repair(data)], tmp_path)
    assert result["skipped"] == [] and len(result["rendered"]) == 1


def test_each_update_must_hit_exactly_one_row_and_the_target_is_cas_checked(tmp_path):
    data = snapshot()
    sql = sql_text(render(data, [repair(data)], tmp_path))
    assert "get diagnostics v_count = row_count; if v_count <> 1 then raise exception 'write_not_one_row'; end if;" in sql


def test_catalog_scalar_patch_keeps_other_fields_and_plan_ids(tmp_path):
    data = snapshot()
    data["memberships"] = [{"plan_id": 461, "business_id": 7, "membership_price": 120,
                            "benefits": None, "is_offer_extracted": False, "updated_at": STAMP}]
    patch = {"table": "clinic_memberships", "id": 461, "business_id": 7, "rule": "benefits",
             "before": deepcopy(data["memberships"][0]), "values": {"benefits": ["10% off products"]}}
    sql = sql_text(render(data, [patch], tmp_path))
    assert """update public.clinic_memberships set benefits = '["10% off products"]'::jsonb where plan_id = 461;""" in sql
    assert "membership_price = " not in sql.split("set constraints all immediate;")[0].split("update public.clinic_memberships")[1].split(";")[0]
    assert """(to_jsonb(t) -> 'benefits') = '["10% off products"]'::jsonb""" in sql
    assert "stale_plan: target_changed clinic_memberships 461" in sql


def test_rendering_does_not_mutate_the_snapshot_or_the_report(tmp_path):
    data = snapshot()
    report = {"repairs": [repair(data)]}
    before_data, before_report = deepcopy(data), deepcopy(report)
    render_audit(report, data, tmp_path, {})
    assert data == before_data and report == before_report


def test_repairs_of_one_business_share_one_block_and_businesses_get_separate_files(tmp_path):
    data = snapshot()
    data["promotions"].append({"promotion_id": 2, "business_id": 8, "source_url": "https://other.example/x",
                               "is_active": True, "updated_at": STAMP})
    data["offers"].append({"id": 2, "business_id": 8, "promotion_id": 2, "membership_plan_id": None, "is_active": True,
                           "updated_at": STAMP})
    second = {"table": "promo_offer_master", "id": 2, "business_id": 8, "rule": "r", "values": {"is_active": False},
              "before": deepcopy(data["offers"][1])}
    item = {"table": "promo_offer_items", "id": 101, "business_id": 7, "rule": "r", "values": {"item_name": "Botox "},
            "before": deepcopy(data["items"][0])}
    result = render(data, [repair(data), item, second], tmp_path)
    assert [r["business_id"] for r in result["rendered"]] == [7, 8] and result["rendered"][0]["repairs"] == 2
    first = open(result["rendered"][0]["sql_path"]).read()
    assert first.index("update public.promo_offer_master") < first.index("update public.promo_offer_items")
    assert "business_id = 8" not in first.split("set constraints")[0].split("update public.promo_offer_master")[1]
    assert len(list_pending(tmp_path / "pending_sql")) == 2


def test_header_carries_plan_digest_and_summary(tmp_path):
    data = snapshot()
    result = render(data, [repair(data)], tmp_path)
    header = read_header(result["rendered"][0]["sql_path"])
    assert header[0].startswith("plan data_quality_7") and re.fullmatch(r"plan sha256 [0-9a-f]{64}", header[1])
    assert any(line.startswith("business_id: 7") for line in header)


def test_satisfied_missing_and_protected_targets_are_not_rendered_and_block_their_business(tmp_path):
    data = snapshot()
    done = repair(data, values={"is_active": True})
    assert render(data, [done], tmp_path)["rendered"] == []
    missing = repair(data, id=99)
    result = render(data, [missing, repair(data)], tmp_path)
    assert result["rendered"] == [] and result["skipped"] == [
        {"table": "promo_offer_master", "id": 99, "business_id": 7, "reason": "target_missing"}]


def test_closure_of_a_promotion_repair_covers_children_and_the_rest_is_version_checked():
    data = snapshot()
    data["offers"].append({"id": 2, "business_id": 7, "promotion_id": None, "membership_plan_id": None,
                           "is_active": True, "updated_at": STAMP})
    patch = {"table": "clinic_promotions", "id": 1, "business_id": 7, "values": {"is_active": False},
             "before": deepcopy(data["promotions"][0])}
    sql = render_business(7, [patch], scope(data, 7))
    offers = next(l for l in sql.splitlines() if "unexpected_row_or_trigger_change: promo_offer_master" in l)
    items = next(l for l in sql.splitlines() if "unexpected_row_or_trigger_change: promo_offer_items" in l)
    # 停用促销会连带停用其 Offer 与子项：它们不参与比较；无关的 Offer 2 必须不变
    assert "(2, '2026-10-01T00:00:00+00:00'::timestamptz)" in offers and "(1," not in offers
    assert "(101," not in items


def test_null_timestamp_in_target_row_is_matched_with_is_not_distinct_from():
    check = row_matches("clinic_memberships", "plan_id", {"plan_id": 760, "created_at": None,
                                                          "updated_at": "2026-10-28T00:50:01.831607+00:00"})
    assert "->> 'created_at')::timestamptz is not distinct from NULL::timestamptz" in check
    assert "= NULL" not in check
