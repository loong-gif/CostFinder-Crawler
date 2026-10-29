from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from utils import pipeline_db
from utils.page_extraction import (
    build_plan,
    database_state,
    latest_scrapes,
    load_decision_pages,
    render_apply,
    snapshot_tables_for_pages,
)
from utils.page_title import TitleUnavailable
from utils.promo_extraction import merge_content_blocks
from utils.promotion_rules import PromotionRejected

BUSINESS = 1
NAMES = {BUSINESS: "Glow Clinic"}
UPDATED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)
EMPTY_STATE = {"names": NAMES, "promotions": {}, "memberships": {}}


def promo_item(title: str, line: str, end: str | None = None) -> dict:
    return {"promotion_title": title, "promotion_content": [line], "campaign_start_date": None,
            "campaign_end_date": end}


class FakeLLM:
    """按页面 URL 返回预设结果，并记录调用次数。"""

    model = "fake-model"

    def __init__(self, by_url: dict[str, dict]) -> None:
        self.by_url = by_url
        self.calls = 0

    def create_json_response(self, messages, json_schema=None, **kwargs):
        self.calls += 1
        user = messages[-1]["content"]
        for url, payload in self.by_url.items():
            if f"Source URL: {url}" in user:
                return payload
        raise AssertionError(f"unexpected page: {user[:200]}")


def scrape(url: str, markdown: str, title: str | None, *, fingerprint: str | None = None) -> dict:
    return {"id": 1, "request_fingerprint": fingerprint or hashlib.sha256(url.encode()).hexdigest(),
            "source_url": url, "success": True,
            "markdown": markdown, "updated_at": "2026-10-29T00:00:00+00:00",
            "metadata": {"title": title} if title is not None else {}}


def promo_llm(url: str, *items: dict) -> dict:
    return {url: {"explanation": "x", "promotions": list(items)}}


def plan_for(pages, scrapes, llm, tmp_path, *, fetch_title=None, state=EMPTY_STATE, manifest=None):
    def no_fetch(url, name):
        raise AssertionError(f"unexpected title fetch: {url}")

    return build_plan(pages, scrapes=latest_scrapes(scrapes), state=state, llm=llm,
                      fetch_title=fetch_title or no_fetch, cache_dir=tmp_path / "cache", manifest=manifest,
                      promotion_schema={}, membership_schema={})


def page(url: str, kind: str = "promotion") -> dict:
    return {"url": url, "business_id": BUSINESS, "kind": kind}


def entry_for(plan: dict, **match) -> dict:
    found = [e for e in plan["entries"] if all(e.get(k) == v for k, v in match.items())]
    assert len(found) == 1, plan["entries"]
    return found[0]


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


def test_hub_children_merge_into_hub_row_with_hub_page_title(tmp_path):
    hub = "https://glow.example/specials"
    a, b = f"{hub}/botox", f"{hub}/filler"
    llm = FakeLLM({**promo_llm(a, promo_item("Botox Bonus", "Save 20% on Botox.")),
                   **promo_llm(b, promo_item("Filler Friday", "Save 20% on Botox."))})
    scrapes = [scrape(a, "Botox Bonus\nSave 20% on Botox.", "Botox child title"),
               scrape(b, "Filler Friday\nSave 20% on Botox.", "Filler child title"),
               scrape(hub, "links only", "Glow Clinic July Offers")]
    plan = plan_for([page(a), page(b)], scrapes, llm, tmp_path)
    row = entry_for(plan, kind="promotion", action="insert")
    assert row["source_url"] == hub
    assert row["title"] == "Glow Clinic July Offers"
    assert row["source_pages"] == [a, b]
    # 每个子页面的优惠整块保留，共有的说明行跟随各自的优惠
    assert row["content"] == ["Botox Bonus", "Save 20% on Botox.", "Filler Friday", "Save 20% on Botox."]
    assert len(plan["entries"]) == 1


def test_product_grid_keeps_each_offer_with_its_own_prices(tmp_path):
    url = "https://glow.example/specials"
    items = [{"promotion_title": "Forehead TOX", "promotion_content": ["Forehead TOX", "$69.00", "-48%", "$135.00"],
              "campaign_start_date": None, "campaign_end_date": None},
             {"promotion_title": "Jelly Roll", "promotion_content": ["Jelly Roll", "$99.00", "-26%", "$135.00"],
              "campaign_start_date": None, "campaign_end_date": None}]
    markdown = ("### Forehead TOX\n$69.00\n-48%\n~~$135.00~~\n### Jelly Roll\n$99.00\n-26%\n~~$135.00~~\n"
                "You may also like: Lip Flip Sale price $69.00 Original price $135.00")
    plan = plan_for([page(url)], [scrape(url, markdown, "Specials | Glow Clinic")],
                    FakeLLM(promo_llm(url, *items)), tmp_path)
    row = entry_for(plan, kind="promotion", action="insert")
    # 各优惠位置上的 $135.00 是删除线原价，页面别处也有不带删除线的 $135.00，仍按位置补回删除线
    assert row["content"] == ["Forehead TOX", "$69.00", "-48%", "~~$135.00~~",
                              "Jelly Roll", "$99.00", "-26%", "~~$135.00~~"]


def test_hub_without_raw_scrape_fetches_hub_title(tmp_path):
    hub = "https://glow.example/specials"
    a = f"{hub}/botox"
    llm = FakeLLM(promo_llm(a, promo_item("Botox Bonus", "Save 20% on Botox.")))
    fetched = []

    def fetch_title(url, name):
        fetched.append((url, name))
        return "Hub Page Title"

    plan = plan_for([page(a)], [scrape(a, "Botox Bonus\nSave 20% on Botox.", "child")], llm, tmp_path,
                    fetch_title=fetch_title)
    assert fetched == [(hub, "Glow Clinic")]
    assert entry_for(plan, action="insert")["title"] == "Hub Page Title"


def test_same_identity_pages_merge_into_one_row(tmp_path):
    a, b = "https://www.glow.example/promo?utm_source=ad", "https://glow.example/promo/"
    llm = FakeLLM({**promo_llm(a, promo_item("Botox Bonus", "Save 20% on Botox.")),
                   **promo_llm(b, promo_item("Botox Bonus", "Save 20% on Botox."))})
    plan = plan_for([page(a), page(b)], [scrape("https://glow.example/promo", "Botox Bonus\nSave 20% on Botox.",
                                                "Glow Clinic Botox Promo")], llm, tmp_path)
    row = entry_for(plan, action="insert")
    assert len(plan["entries"]) == 1
    assert row["source_pages"] == [b, a] or row["source_pages"] == [a, b]
    assert row["content"] == ["Botox Bonus", "Save 20% on Botox."]


def test_expired_campaign_is_dropped(tmp_path):
    url = "https://glow.example/promo"
    md = "Botox Bonus\nSave 20% on Botox through January 31, 2020."
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox through January 31, 2020.", "2020-01-31")))
    plan = plan_for([page(url)], [scrape(url, md, "Glow Clinic Botox Promo")], llm, tmp_path)
    row = entry_for(plan, action="rejected")
    assert row["reason"] == "expired_campaign"
    assert row["campaign_end_date"] == "2020-01-31"


def test_unknown_dates_are_kept_as_null(tmp_path):
    url = "https://glow.example/promo"
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    plan = plan_for([page(url)], [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Glow Clinic Botox Promo")], llm, tmp_path)
    row = entry_for(plan, action="insert")
    assert row["campaign_start_date"] is None and row["campaign_end_date"] is None


def test_price_list_page_without_discount_is_rejected(tmp_path):
    url = "https://glow.example/pricing"
    llm = FakeLLM(promo_llm(url, promo_item("Botox Pricing", "Botox $12 per unit")))
    plan = plan_for([page(url)], [scrape(url, "Botox Pricing\nBotox $12 per unit", "Glow Clinic Pricing List")],
                    llm, tmp_path)
    assert entry_for(plan, action="rejected")["reason"] == "price_page_without_discount"


def test_title_comes_from_scrape_metadata_not_llm(tmp_path):
    url = "https://glow.example/promo"
    llm = FakeLLM(promo_llm(url, promo_item("LLM Invented Title", "Save 20% on Botox.")))
    md = "LLM Invented Title\nSave 20% on Botox."
    plan = plan_for([page(url)], [scrape(url, md, "Glow Clinic | Botox Deals Page")], llm, tmp_path)
    assert entry_for(plan, action="insert")["title"] == "Glow Clinic | Botox Deals Page"


def test_page_without_usable_title_is_rejected(tmp_path):
    url = "https://glow.example/promo"
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    plan = plan_for([page(url)], [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Home")], llm, tmp_path)
    assert entry_for(plan, action="rejected")["reason"] == "no_usable_title"


def test_hub_title_fetch_failure_rejects_group(tmp_path):
    a = "https://glow.example/specials/botox"
    llm = FakeLLM(promo_llm(a, promo_item("Botox Bonus", "Save 20% on Botox.")))

    def fetch_title(url, name):
        raise TitleUnavailable("http_404")

    plan = plan_for([page(a)], [scrape(a, "Botox Bonus\nSave 20% on Botox.", "child")], llm, tmp_path,
                    fetch_title=fetch_title)
    assert entry_for(plan, action="rejected")["reason"] == "http_404"


def test_missing_scrape_and_llm_cache(tmp_path):
    url = "https://glow.example/promo"
    scrapes = [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Glow Clinic Botox Promo")]
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    missing = "https://glow.example/other"
    first = plan_for([page(url), page(missing)], scrapes, llm, tmp_path)
    assert entry_for(first, source_url=missing)["reason"] == "no_scrape"
    assert llm.calls == 1
    plan_for([page(url)], scrapes, llm, tmp_path)
    assert llm.calls == 1


def test_existing_rows_decide_update_and_unchanged(tmp_path):
    url = "https://glow.example/promo"
    scrapes = [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Glow Clinic Botox Promo")]
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    key = (BUSINESS, "glow.example/promo")
    old = {"promotion_id": 7, "promotion_title": "Glow Clinic Botox Promo",
           "promotion_content": ["Botox Bonus", "Save 20% on Botox."], "campaign_start_date": None,
           "campaign_end_date": None, "is_active": True, "updated_at": UPDATED_AT}
    same = plan_for([page(url)], scrapes, llm, tmp_path, state={**EMPTY_STATE, "promotions": {key: old}})
    assert entry_for(same, kind="promotion")["action"] == "unchanged"
    changed = {**old, "promotion_content": ["Botox Bonus"]}
    diff = plan_for([page(url)], scrapes, llm, tmp_path, state={**EMPTY_STATE, "promotions": {key: changed}})
    row = entry_for(diff, kind="promotion")
    assert (row["action"], row["promotion_id"], row["content_changed"]) == ("update", 7, True)


def test_visibility_manifest_filters_content_and_rejects_invisible_pages(tmp_path):
    url = "https://glow.example/promo"
    scrapes = [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Glow Clinic Botox Promo")]
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    visible = {"status": "page_accessible", "claimResults": [
        {"recordType": "promotion", "claim": "Save 20% on Botox.", "status": "visible_match"}]}
    plan = plan_for([page(url)], scrapes, llm, tmp_path, manifest={url: visible})
    assert plan["visibility_manifest_sha256"] is not None
    assert entry_for(plan, action="insert")["content"] == ["Save 20% on Botox."]
    rejected = plan_for([page(url)], scrapes, llm, tmp_path, manifest={})
    assert entry_for(rejected, action="rejected")["reason"] == "not_in_visibility_manifest"
    hidden = {"status": "page_accessible", "claimResults": []}
    invisible = plan_for([page(url)], scrapes, llm, tmp_path, manifest={url: hidden})
    assert entry_for(invisible, action="rejected")["reason"] == "no_rendered_visible_promotion_content"


def membership_item(name: str, price: float) -> dict:
    return {"membership_name": name, "membership_price": price, "billing_period": "monthly",
            "minimum_commitment_months": None, "membership_description": None, "benefits": None}


def test_membership_identity_merges_case_variants_and_rejects_conflicts(tmp_path):
    url = "https://glow.example/membership/"
    md = "Glow Club $99 per month\nGlow Plus $150 per month, or $175 per month\n"
    payload = {url: {"explanation": "x", "memberships": [
        membership_item("Glow Club", 99), membership_item("glow club", 99), membership_item("Glow Plus", 150),
        membership_item("GLOW PLUS", 175)]}}
    plan = plan_for([page(url, "membership")], [scrape(url, md, "Members")], FakeLLM(payload), tmp_path)
    inserted = [e for e in plan["entries"] if e["action"] == "insert"]
    assert [e["membership_name"] for e in inserted] == ["Glow Club"]
    assert inserted[0]["source_url"] == "https://glow.example/membership"
    assert inserted[0]["target_key"] == {"business_id": BUSINESS, "source_url_key": "glow.example/membership",
                                          "membership_name_key": "glow club"}
    conflicts = [e for e in plan["entries"] if e["reason"] == "conflicting_duplicate_membership"]
    assert sorted(e["membership_price"] for e in conflicts) == [150, 175]


def test_membership_fee_not_supported_is_rejected(tmp_path):
    url = "https://glow.example/membership"
    payload = {url: {"explanation": "x", "memberships": [membership_item("Glow Club", 99)]}}
    plan = plan_for([page(url, "membership")], [scrape(url, "no prices here", "Members")], FakeLLM(payload), tmp_path)
    assert entry_for(plan, action="rejected")["reason"] == "membership_fee_not_supported_by_named_evidence"


def test_plan_is_json_serializable_and_summarized(tmp_path):
    import json

    url = "https://glow.example/promo"
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    plan = plan_for([page(url)], [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Glow Clinic Botox Promo")], llm, tmp_path)
    loaded = json.loads(json.dumps(plan))
    assert loaded["summary"] == {"promotion:insert": 1}
    row = loaded["entries"][0]
    assert {"business_id", "source_url", "target_key", "title", "content", "campaign_start_date",
            "campaign_end_date", "action", "reason"} <= set(row)


VISIBLE = {"status": "page_accessible", "claimResults": [
    {"recordType": "promotion", "claim": "Save 20% on Botox.", "status": "visible_match"},
    {"recordType": "promotion", "claim": "Botox Bonus", "status": "visible_match"}]}


def test_render_apply_requires_same_manifest_and_upserts_by_identity(tmp_path):
    url = "https://glow.example/promo"
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    scrapes = [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Glow Clinic Botox Promo")]
    unchecked = plan_for([page(url)], scrapes, llm, tmp_path)
    with pytest.raises(RuntimeError, match="plan_without_visibility_manifest"):
        render_apply(unchecked, {})
    manifest = {url: VISIBLE}
    plan = plan_for([page(url)], scrapes, llm, tmp_path, manifest=manifest)
    with pytest.raises(RuntimeError, match="visibility_manifest_changed"):
        render_apply(plan, {})
    text = render_apply(json.loads(json.dumps(plan)), manifest)
    assert text.count("do $pipeline$") == 1 and text.rstrip().endswith("$pipeline$;")
    assert "on conflict (business_id, public.fn_source_url_key(source_url))" in text
    assert "'https://glow.example/promo'" in text and "'Glow Clinic Botox Promo'" in text
    assert "'[\"Botox Bonus\", \"Save 20% on Botox.\"]'::jsonb" in text
    # 计划时目标行不存在：先核对仍不存在，再 upsert，并要求确实是插入
    assert "perform 1 from public.clinic_promotions where business_id = 1" in text
    assert text.index("raise exception 'stale_plan") < text.index("insert into public.clinic_promotions")
    assert "if v_inserted is distinct from true then raise exception" in text
    assert f"visibility manifest sha256 {plan['visibility_manifest_sha256']}" in text


def test_render_apply_checks_updated_at_for_existing_rows_and_memberships(tmp_path):
    url = "https://glow.example/promo"
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    manifest = {url: VISIBLE}
    old = {"promotion_id": 7, "promotion_title": "Old", "promotion_content": ["x"], "campaign_start_date": None,
           "campaign_end_date": None, "is_active": True, "updated_at": UPDATED_AT}
    state = {**EMPTY_STATE, "promotions": {(BUSINESS, "glow.example/promo"): old}}
    plan = plan_for([page(url)], [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Glow Clinic Botox Promo")], llm,
                    tmp_path, manifest=manifest, state=state)
    text = render_apply(plan, manifest)
    assert "'2026-10-01T00:00:00+00:00'::timestamptz" in text and "for update;" in text
    assert "if v_inserted is distinct from false then raise exception" in text

    murl = "https://glow.example/membership"
    mmd = "Glow Club $99 per month\n"
    payload = {murl: {"explanation": "x", "memberships": [membership_item("Glow Club", 99)]}}
    mold = {"plan_id": 3, "membership_name": "Glow Club", "membership_price": 80.0, "billing_period": "monthly",
            "minimum_commitment_months": None, "benefits": None, "updated_at": UPDATED_AT}
    mstate = {**EMPTY_STATE, "memberships": {(BUSINESS, "glow.example/membership", "glow club"): mold}}
    mvisible = {"status": "page_accessible", "claimResults": []}
    mplan = plan_for([page(murl, "membership")], [scrape(murl, mmd, "Members")], FakeLLM(payload), tmp_path,
                     manifest={murl: mvisible}, state=mstate)
    mtext = render_apply(mplan, {murl: mvisible})
    assert "lower(btrim(membership_name)) = lower(btrim('Glow Club'))" in mtext
    assert "on conflict (business_id, public.fn_source_url_key(source_url), lower(btrim(membership_name)))" in mtext
    assert "'2026-10-01T00:00:00+00:00'::timestamptz" in mtext


def test_render_apply_reruns_promotion_rules(tmp_path):
    url = "https://glow.example/promo"
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    manifest = {url: VISIBLE}
    plan = plan_for([page(url)], [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Glow Clinic Botox Promo")], llm,
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


def test_hub_page_in_batch_keeps_hub_title(tmp_path):
    hub = "https://glow.example/specials"
    child = f"{hub}/botox"
    # FakeLLM 按子串匹配 URL，子页面需排在 hub 之前
    llm = FakeLLM({**promo_llm(child, promo_item("Botox Bonus", "Save 20% on Botox.")),
                   **promo_llm(hub, promo_item("Fall Specials", "Save 20% on fillers."))})
    scrapes = [scrape(hub, "Fall Specials\nSave 20% on fillers.", "Specials | Glow Clinic"),
               scrape(child, "Botox Bonus\nSave 20% on Botox.", "Botox child title")]
    row = entry_for(plan_for([page(hub), page(child)], scrapes, llm, tmp_path), action="insert")
    assert (row["source_url"], row["title"]) == (hub, "Specials | Glow Clinic")
    assert row["source_pages"] == [hub, child]


def test_expired_child_is_not_merged_into_hub(tmp_path):
    hub = "https://glow.example/specials"
    old_child, live_child = f"{hub}/valentines", f"{hub}/botox"
    llm = FakeLLM({**promo_llm(old_child, promo_item("Valentine Glow", "Save 30% through February 14, 2020.",
                                                     "2020-02-14")),
                   **promo_llm(live_child, promo_item("Botox Bonus", "Save 20% on Botox."))})
    scrapes = [scrape(old_child, "Valentine Glow\nSave 30% through February 14, 2020.", "Valentine"),
               scrape(live_child, "Botox Bonus\nSave 20% on Botox.", "Botox"),
               scrape(hub, "links", "Specials | Glow Clinic")]
    plan = plan_for([page(old_child), page(live_child)], scrapes, llm, tmp_path)
    assert entry_for(plan, source_url=old_child)["reason"] == "expired_campaign"
    row = entry_for(plan, action="insert")
    assert "Valentine Glow" not in row["content"] and row["source_pages"] == [live_child]


def test_partial_hub_batch_keeps_existing_hub_content(tmp_path):
    hub = "https://glow.example/specials"
    child = f"{hub}/botox"
    llm = FakeLLM(promo_llm(child, promo_item("Botox Bonus", "Save 20% on Botox.")))
    old = {"promotion_id": 9, "promotion_title": "Specials | Glow Clinic",
           "promotion_content": ["Filler Friday", "Save $100 on filler."], "campaign_start_date": None,
           "campaign_end_date": None, "is_active": True, "updated_at": UPDATED_AT}
    state = {**EMPTY_STATE, "promotions": {(BUSINESS, "glow.example/specials"): old}}
    scrapes = [scrape(child, "Botox Bonus\nSave 20% on Botox.", "Botox"), scrape(hub, "links", "Specials | Glow Clinic")]
    row = entry_for(plan_for([page(child)], scrapes, llm, tmp_path, state=state), kind="promotion")
    assert row["action"] == "update"
    assert row["content"] == ["Filler Friday", "Save $100 on filler.", "Botox Bonus", "Save 20% on Botox."]


def test_markdown_variants_of_one_line_are_merged_within_a_block():
    block = ["* **Buy 2 Radiesse, get the 3rd half off** (save $475).",
             "**Buy 2 Radiesse, get the 3rd half off** (save $475).",
             "*Regularly $3,500**", "Regularly $3,500"]
    assert merge_content_blocks([block]) == ["Buy 2 Radiesse, get the 3rd half off (save $475).", "Regularly $3,500"]


def test_repeated_block_is_dropped_but_shared_short_lines_stay_with_each_offer():
    blocks = [["Forehead TOX", "Regular price", "$69.00", "$135.00"],
              ["Jelly Roll", "Regular price", "$99.00", "$135.00"],
              ["**Forehead TOX**", "Regular price", "$69.00", "$135.00"]]
    assert merge_content_blocks(blocks) == ["Forehead TOX", "Regular price", "$69.00", "$135.00",
                                            "Jelly Roll", "Regular price", "$99.00", "$135.00"]


def test_specials_title_with_business_name_is_accepted(tmp_path):
    url = "https://glow.example/specials"
    llm = FakeLLM(promo_llm(url, promo_item("Botox Bonus", "Save 20% on Botox.")))
    plan = plan_for([page(url)], [scrape(url, "Botox Bonus\nSave 20% on Botox.", "Specials | Glow Clinic")],
                    llm, tmp_path)
    assert entry_for(plan, action="insert")["title"] == "Specials | Glow Clinic"
