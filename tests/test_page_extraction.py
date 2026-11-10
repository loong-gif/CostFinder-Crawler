from __future__ import annotations

import json

import pytest

from page_extraction_support import (
    BOTOX,
    BOTOX_PAGE,
    BUSINESS,
    EMPTY_STATE,
    NAMES,
    UPDATED_AT,
    membership_item,
    membership_llm,
    membership_review,
    page,
    plan_for,
    promo_llm,
    scrape,
    visible,
)
from utils import pipeline_db
from utils.page_extraction import render_apply
from utils.page_extraction_snapshot import database_state, load_decision_pages, snapshot_tables_for_pages
from utils.promotion_rules import PromotionRejected


def test_load_decision_pages_keeps_added_pages_and_maps_kinds():
    payload = {"decisions": [
        {"url": "https://a.example/specials", "business_id": 1, "kind": "promotion", "decision": "add"},
        {"url": "https://a.example/membership", "business_id": 1, "kind": "membership", "decision": "add"},
        {"url": "https://a.example/menu", "business_id": 1, "kind": "pricing", "decision": "add"},
        {"url": "https://a.example/old", "business_id": 1, "kind": "promotion", "decision": "skip"},
        {"url": "https://b.example/specials", "business_id": 2, "kind": "promotion", "decision": "add"},
    ]}
    pages = load_decision_pages(payload, business_ids={1})
    assert [(p["url"], p["kind"]) for p in pages] == [
        ("https://a.example/membership", "membership"), ("https://a.example/menu", "promotion"),
        ("https://a.example/specials", "promotion")]
    assert len(load_decision_pages(payload, limit=1)) == 1


def test_plan_is_json_serializable_and_summarized(tmp_path):
    url = "https://glow.example/promo"
    plan = plan_for([page(url)], [scrape(url, BOTOX, "Glow Clinic Botox Promo")], promo_llm({url: BOTOX_PAGE}), tmp_path)
    loaded = json.loads(json.dumps(plan))
    assert loaded["summary"] == {"promotion:insert": 1}
    row = loaded["entries"][0]
    assert {"business_id", "source_url", "target_key", "title", "content", "campaign_start_date",
            "campaign_end_date", "action", "reason"} <= set(row)


def test_render_apply_requires_same_manifest_and_upserts_by_identity(tmp_path):
    url = "https://glow.example/promo"
    llm = promo_llm({url: BOTOX_PAGE})
    scrapes = [scrape(url, BOTOX, "Glow Clinic Botox Promo")]
    unchecked = plan_for([page(url)], scrapes, llm, tmp_path)
    with pytest.raises(RuntimeError, match="plan_without_visibility_manifest"):
        render_apply(unchecked, {})
    manifest = {url: visible(BOTOX)}
    plan = plan_for([page(url)], scrapes, llm, tmp_path, manifest=manifest)
    with pytest.raises(RuntimeError, match="visibility_manifest_changed"):
        render_apply(plan, {})
    text = render_apply(json.loads(json.dumps(plan)), manifest)
    assert text.count("do $pipeline$") == 1 and text.rstrip().endswith("$pipeline$;")
    assert "on conflict (business_id, public.fn_source_url_key(source_url))" in text
    assert "'https://glow.example/promo'" in text and "'Glow Clinic Botox Promo'" in text
    assert "E'[\"Botox Bonus\\\\nSave 20% on Botox.\"]'::jsonb" in text
    # 计划时目标行不存在：先核对仍不存在，再 upsert，并要求确实是插入
    assert "perform 1 from public.clinic_promotions where business_id = 1" in text
    assert text.index("raise exception 'stale_plan") < text.index("insert into public.clinic_promotions")
    assert "if v_inserted is distinct from true then raise exception" in text
    assert f"visibility manifest sha256 {plan['visibility_manifest_sha256']}" in text


def test_branches_sharing_a_page_each_get_a_promotion_written_in_one_loop(tmp_path):
    url = "https://glow.example/promo"
    manifest = {url: visible(BOTOX)}
    state = {**EMPTY_STATE, "names": {1: "Glow Clinic Irvine", 2: "Glow Clinic Tustin", 3: "Glow Clinic Orange"}}
    pages = load_decision_pages({"decisions": [{"url": url, "business_id": b, "decision": "add", "kind": "promotion"}
                                               for b in (1, 2, 3, 2)]})
    assert [p["business_id"] for p in pages] == [1, 2, 3]
    plan = plan_for(pages, [scrape(url, BOTOX, "Glow Clinic Botox Promo")], promo_llm({url: BOTOX_PAGE}),
                    tmp_path, manifest=manifest, state=state)
    assert [(e["business_id"], e["action"]) for e in plan["entries"]] == [(1, "insert"), (2, "insert"), (3, "insert")]
    assert len({json.dumps(e["content"]) for e in plan["entries"]}) == 1
    text = render_apply(plan, manifest)
    # 三家分店的内容只写一次，按商家循环写入，循环内逐家核对
    assert text.count("insert into public.clinic_promotions") == 1
    assert "for v_i in 1..3 loop" in text and "v_business_id := ('{1,2,3}'::int2[]::bigint[])[v_i];" in text
    assert "where business_id = v_business_id and public.fn_source_url_key(source_url)" in text
    assert "writes {\"promotion:insert\": 3}" in text

    # 已有行的同内容更新同样合并：逐家锁定并核对计划时的 updated_at，写入结果必须是更新
    old = {"promotion_id": 7, "promotion_title": "Old", "promotion_content": ["x"], "campaign_start_date": None,
           "campaign_end_date": None, "is_active": True, "updated_at": UPDATED_AT}
    state = {**state, "promotions": {(b, "glow.example/promo"): {**old, "promotion_id": 6 + b} for b in (1, 2, 3)}}
    plan = plan_for(pages, [scrape(url, BOTOX, "Glow Clinic Botox Promo")], promo_llm({url: BOTOX_PAGE}),
                    tmp_path / "update", manifest=manifest, state=state)
    text = render_apply(plan, manifest)
    assert text.count("insert into public.clinic_promotions") == 1 and "for update;" in text
    assert "is distinct from ('{2026-10-01T00:00:00+00:00" in text and "::timestamptz[])[v_i]" in text
    assert "if v_inserted is distinct from false then raise exception" in text
    assert "writes {\"promotion:update\": 3}" in text


def test_render_apply_checks_updated_at_for_existing_rows_and_memberships(tmp_path):
    url = "https://glow.example/promo"
    manifest = {url: visible(BOTOX)}
    old = {"promotion_id": 7, "promotion_title": "Old", "promotion_content": ["x"], "campaign_start_date": None,
           "campaign_end_date": None, "is_active": True, "updated_at": UPDATED_AT}
    state = {**EMPTY_STATE, "promotions": {(BUSINESS, "glow.example/promo"): old}}
    plan = plan_for([page(url)], [scrape(url, BOTOX, "Glow Clinic Botox Promo")], promo_llm({url: BOTOX_PAGE}),
                    tmp_path, manifest=manifest, state=state)
    text = render_apply(plan, manifest)
    assert "'2026-10-01T00:00:00+00:00'::timestamptz" in text and "for update;" in text
    assert "if v_inserted is distinct from false then raise exception" in text

    murl = "https://glow.example/membership"
    mold = {"plan_id": 3, "membership_name": "Glow Club", "membership_price": 80.0, "billing_period": "monthly",
            "minimum_commitment_months": None, "benefits": None, "updated_at": UPDATED_AT}
    mstate = {**EMPTY_STATE, "memberships": {(BUSINESS, "glow.example/membership", "glow club"): mold}}
    mvisible = {"status": "page_accessible", "claimResults": [
        {"recordType": "membership", "claim": claim, "status": "visible_match"} for claim in ("Glow Club", "Glow Club $99")]}
    mllm = membership_llm(murl, [membership_item("Glow Club", 99)], membership_review(("Glow Club", [])))
    mplan = plan_for([page(murl, "membership")], [scrape(murl, "Glow Club $99 per month\n", "Members")], mllm, tmp_path,
                     manifest={murl: mvisible}, state=mstate)
    mtext = render_apply(mplan, {murl: mvisible})
    assert "lower(btrim(membership_name)) = lower(btrim('Glow Club'))" in mtext
    assert "on conflict (business_id, public.fn_source_url_key(source_url), lower(btrim(membership_name)))" in mtext
    assert "'2026-10-01T00:00:00+00:00'::timestamptz" in mtext


def test_render_apply_reruns_promotion_rules(tmp_path):
    url = "https://glow.example/promo"
    manifest = {url: visible(BOTOX)}
    plan = plan_for([page(url)], [scrape(url, BOTOX, "Glow Clinic Botox Promo")], promo_llm({url: BOTOX_PAGE}),
                    tmp_path, manifest=manifest)
    next(e for e in plan["entries"] if e["action"] == "insert")["campaign_end_date"] = "2020-01-31"
    with pytest.raises(PromotionRejected, match="expired_campaign"):
        render_apply(plan, manifest)


def test_snapshot_tables_and_database_state_from_snapshot(tmp_path):
    tables = snapshot_tables_for_pages([page("https://glow.example/a"), page("https://glow.example/b")])
    assert set(tables) == {"businesses", "promotions", "memberships"}
    with pytest.raises(RuntimeError, match="no_pages"):
        snapshot_tables_for_pages([])
    read_sql = pipeline_db.snapshot_query(tables)
    assert "public.master_business_info" in read_sql and "public.clinic_memberships" in read_sql
    assert "'{1}'::int2[]::bigint[]" in read_sql
    snapshot = {"businesses": [{"business_id": 1, "name": "Glow Clinic"}],
                "promotions": [{"promotion_id": 7, "business_id": 1, "source_url": "https://www.glow.example/promo/"}],
                "memberships": [{"plan_id": 3, "business_id": 1, "source_url": "https://glow.example/m",
                                 "membership_name": " Glow Club "}]}
    state = database_state(snapshot, [1])
    assert state["names"] == NAMES and list(state["promotions"]) == [(1, "glow.example/promo")]
    assert list(state["memberships"]) == [(1, "glow.example/m", "glow club")]
    with pytest.raises(RuntimeError, match="unknown_business_ids"):
        database_state(snapshot, [1, 2])
    duplicate = {**snapshot, "promotions": snapshot["promotions"] * 2}
    with pytest.raises(RuntimeError, match="duplicate_promotion_identity"):
        database_state(duplicate, [1])
