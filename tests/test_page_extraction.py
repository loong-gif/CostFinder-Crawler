from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from utils import pipeline_db
from utils.page_extraction import (
    SOURCE_REVIEW_SCHEMA,
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


def promo_item(title: str, *lines: str, end: str | None = None) -> dict:
    return {"promotion_title": title, "promotion_content": list(lines), "campaign_start_date": None,
            "campaign_end_date": end}


class FakeLLM:
    """按页面 URL 返回预设结果：来源复核请求返回 reviews，抽取请求返回 payloads；记录调用次数。"""

    model = "fake-model"

    def __init__(self, payloads: dict[str, dict], reviews: dict[str, dict]) -> None:
        self.payloads = payloads
        self.reviews = reviews
        self.calls = 0

    def create_json_response(self, messages, json_schema=None, **kwargs):
        self.calls += 1
        user = messages[-1]["content"]
        answers = self.reviews if json_schema is SOURCE_REVIEW_SCHEMA else self.payloads
        for url, payload in answers.items():
            if f"Source URL: {url}\n" in user:
                return payload
        raise AssertionError(f"unexpected request: {user[:200]}")


def promo_review(*cards: tuple[str, list[str]], page_type: str = "promotion") -> dict:
    """来源复核：每张卡片为 (标题, 其余原文行)。"""
    return {"page_type": page_type, "memberships": [],
            "promotion_quotes": [{"title": title, "lines": lines} for title, lines in cards]}


def membership_review(*plans: tuple[str, list[str]]) -> dict:
    return {"page_type": "membership", "promotion_quotes": [],
            "memberships": [{"name": name, "source_claims": claims} for name, claims in plans]}


def promo_llm(pages: dict[str, tuple[list[dict], dict]]) -> FakeLLM:
    """pages: URL → (抽取出的优惠, 来源复核)。"""
    return FakeLLM({url: {"explanation": "x", "promotions": items} for url, (items, _) in pages.items()},
                   {url: review for url, (_, review) in pages.items()})


def scrape(url: str, markdown: str, title: str | None, *, fingerprint: str | None = None) -> dict:
    return {"id": 1, "request_fingerprint": fingerprint or hashlib.sha256(url.encode()).hexdigest(),
            "source_url": url, "success": True,
            "markdown": markdown, "updated_at": "2026-10-29T00:00:00+00:00",
            "metadata": {"title": title} if title is not None else {}}


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


BOTOX = "Botox Bonus\nSave 20% on Botox."
BOTOX_PAGE = ([promo_item("Botox Bonus", "Save 20% on Botox.")], promo_review(("Botox Bonus", ["Save 20% on Botox."])))


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
    llm = promo_llm({a: BOTOX_PAGE,
                     b: ([promo_item("Filler Friday", "Save 20% on Botox.")],
                         promo_review(("Filler Friday", ["Save 20% on Botox."])))})
    scrapes = [scrape(a, BOTOX, "Botox child title"),
               scrape(b, "Filler Friday\nSave 20% on Botox.", "Filler child title"),
               scrape(hub, "links only", "Glow Clinic July Offers")]
    plan = plan_for([page(a), page(b)], scrapes, llm, tmp_path)
    row = entry_for(plan, kind="promotion", action="insert")
    assert row["source_url"] == hub
    assert row["title"] == "Glow Clinic July Offers"
    assert row["source_pages"] == [a, b]
    # 每个优惠是一个条目，共有的说明行跟随各自的优惠
    assert row["content"] == [BOTOX, "Filler Friday\nSave 20% on Botox."]
    assert len(plan["entries"]) == 1


def test_product_grid_keeps_each_offer_with_its_own_struck_original_price(tmp_path):
    url = "https://glow.example/specials"
    items = [promo_item("Forehead TOX", "Forehead TOX", "$69.00", "-48%", "$135.00"),
             promo_item("Jelly Roll", "Jelly Roll", "$99.00", "-26%", "$135.00")]
    review = promo_review(("Forehead TOX", ["$69.00", "-48%", "$135.00"]), ("Jelly Roll", ["$99.00", "-26%", "$135.00"]))
    markdown = ("### Forehead TOX\n$69.00\n-48%\n~~$135.00~~\n### Jelly Roll\n$99.00\n-26%\n~~$135.00~~\n"
                "You may also like: Lip Flip Sale price $69.00 Original price $135.00")
    plan = plan_for([page(url)], [scrape(url, markdown, "Specials | Glow Clinic")],
                    promo_llm({url: (items, review)}), tmp_path)
    row = entry_for(plan, kind="promotion", action="insert")
    # 各优惠位置上的 $135.00 是删除线原价，页面别处也有不带删除线的 $135.00，仍按位置保留源页面的删除线
    assert row["content"] == ["Forehead TOX\n$69.00\n-48%\n~~$135.00~~", "Jelly Roll\n$99.00\n-26%\n~~$135.00~~"]


@pytest.mark.parametrize("struck", ["$135", "~$135~", "~~$135~~"])
def test_struck_original_price_is_the_discount_evidence_on_a_plain_service_page(tmp_path, struck):
    url = "https://glow.example/botox"
    markdown = "### Forehead TOX\n$69\n~~$135~~"
    # LLM 返回的原价可能去掉或只保留一部分删除线记号，写入的总是源页面原文
    llm = promo_llm({url: ([promo_item("Forehead TOX", "$69", struck)], promo_review(("Forehead TOX", ["$69", "$135"])))})
    row = entry_for(plan_for([page(url)], [scrape(url, markdown, "Botox | Glow Clinic")], llm, tmp_path),
                    kind="promotion")
    assert (row["action"], row["content"]) == ("insert", ["Forehead TOX\n$69\n~~$135~~"])


def test_card_price_must_agree_with_the_source_card(tmp_path):
    url = "https://glow.example/specials"
    markdown = "Filler\n$500 off\nfiller from $50"
    # 引文 "$50" 是 "$500 off" 的子串，但两张卡片的金额不同
    llm = promo_llm({url: ([promo_item("Filler", "$500 off")], promo_review(("Filler", ["$50"])))})
    plan = plan_for([page(url)], [scrape(url, markdown, "Specials | Glow Clinic")], llm, tmp_path)
    assert entry_for(plan, kind="promotion")["reason"] == "promotion_source_inventory_mismatch"


def test_extracted_card_must_pair_with_exactly_one_source_card(tmp_path):
    url = "https://glow.example/specials"
    markdown = "Botox Bonus\nSave 20% on Botox.\nBotox Bonus Plus\nSave 20% on Botox.\nSpring Filler\nSave 20% on filler."
    scrapes = [scrape(url, markdown, "Specials | Glow Clinic")]
    items = [promo_item("Botox", "Save 20% on Botox.")]
    # 复核中重复列出的同一张卡片只算一张；复核多出的卡片不阻止写入，记录为未抽取
    duplicated = promo_review(("Botox Bonus", ["Save 20% on Botox."]), ("Botox Bonus", ["Save 20% on Botox."]),
                              ("Spring Filler", ["Save 20% on filler."]))
    row = entry_for(plan_for([page(url)], scrapes, promo_llm({url: (items, duplicated)}), tmp_path / "a"),
                    kind="promotion")
    assert (row["action"], row["content"]) == ("insert", ["Botox\nSave 20% on Botox."])
    assert row["source_reviews"][0]["source_cards_not_extracted"] == ["Spring Filler"]
    # 标题 "Botox" 同时包含于两张不同的复核卡片，无法唯一对应
    ambiguous = promo_review(("Botox Bonus", ["Save 20% on Botox."]), ("Botox Bonus Plus", ["Save 20% on Botox."]))
    row = entry_for(plan_for([page(url)], scrapes, promo_llm({url: (items, ambiguous)}), tmp_path / "b"),
                    kind="promotion")
    assert row["reason"] == "promotion_source_inventory_mismatch"


def test_card_missing_from_the_source_review_is_not_written(tmp_path):
    url = "https://glow.example/specials"
    markdown = f"{BOTOX}\nReferral Bonus\nGet $50 when a friend books."
    items = [promo_item("Botox Bonus", "Save 20% on Botox."), promo_item("Referral Bonus", "Get $50 when a friend books.")]
    plan = plan_for([page(url)], [scrape(url, markdown, "Specials | Glow Clinic")],
                    promo_llm({url: (items, BOTOX_PAGE[1])}), tmp_path)
    row = entry_for(plan, kind="promotion")
    assert (row["action"], row["content"]) == ("insert", [BOTOX])
    assert row["source_reviews"][0]["cards_not_in_source_review"] == ["Referral Bonus"]


def test_review_title_with_the_price_pairs_with_the_extracted_card(tmp_path):
    url = "https://glow.example/specials"
    markdown = "Beauty Bank Basic Membership – $300/month\n$200/month!\n(for 6 months for 3 treatments)"
    items = [promo_item("Beauty Bank Basic Membership", "$200/month!", "(for 6 months for 3 treatments)",
                        "Beauty Bank Basic Membership – $300/month")]
    review = promo_review(("Beauty Bank Basic Membership – $300/month", ["$200/month!", "(for 6 months for 3 treatments)"]))
    plan = plan_for([page(url)], [scrape(url, markdown, "Specials | Glow Clinic")], promo_llm({url: (items, review)}),
                    tmp_path)
    assert entry_for(plan, kind="promotion")["action"] == "insert"


def test_paraphrased_source_review_rejects_only_its_page_and_is_not_requested_again(tmp_path):
    good, bad = "https://glow.example/specials", "https://other.example/specials"
    markdown = "Spring Glow\n*Limited\ntime offer*\nSave 20% on Botox."
    llm = promo_llm({
        # 跨行的强调标记：复核引文 "*Limited" 与页面逐行渲染后一致
        good: ([promo_item("Spring Glow", "Save 20% on Botox.")],
               promo_review(("Spring Glow", ["*Limited", "Save 20% on Botox."]))),
        bad: (BOTOX_PAGE[0], promo_review(("Botox Bonus", ["Save twenty percent on Botox"])))})
    scrapes = [scrape(good, markdown, "Specials | Glow Clinic"), scrape(bad, BOTOX, "Specials | Other Clinic")]
    pages = [page(good), {**page(bad), "business_id": 2}]
    state = {**EMPTY_STATE, "names": {**NAMES, 2: "Other Clinic"}}
    plan = plan_for(pages, scrapes, llm, tmp_path, state=state)
    assert entry_for(plan, source_url=bad)["reason"] == "promotion_source_quote_not_verbatim"
    assert entry_for(plan, source_url=good)["content"] == ["Spring Glow\nSave 20% on Botox."]
    calls = llm.calls
    again = plan_for(pages, scrapes, llm, tmp_path, state=state)
    assert entry_for(again, source_url=bad)["reason"] == "promotion_source_quote_not_verbatim"
    assert llm.calls == calls


def test_hub_without_raw_scrape_fetches_hub_title(tmp_path):
    hub = "https://glow.example/specials"
    a = f"{hub}/botox"
    fetched = []

    def fetch_title(url, name):
        fetched.append((url, name))
        return "Hub Page Title"

    plan = plan_for([page(a)], [scrape(a, BOTOX, "child")], promo_llm({a: BOTOX_PAGE}), tmp_path,
                    fetch_title=fetch_title)
    assert fetched == [(hub, "Glow Clinic")]
    assert entry_for(plan, action="insert")["title"] == "Hub Page Title"


def test_same_identity_pages_merge_into_one_row(tmp_path):
    a, b = "https://www.glow.example/promo?utm_source=ad", "https://glow.example/promo/"
    llm = promo_llm({a: BOTOX_PAGE, b: BOTOX_PAGE})
    plan = plan_for([page(a), page(b)], [scrape("https://glow.example/promo", BOTOX, "Glow Clinic Botox Promo")],
                    llm, tmp_path)
    row = entry_for(plan, action="insert")
    assert len(plan["entries"]) == 1
    assert row["source_pages"] == [b, a] or row["source_pages"] == [a, b]
    assert row["content"] == [BOTOX]


def test_expired_campaign_is_dropped(tmp_path):
    url = "https://glow.example/promo"
    line = "Save 20% on Botox through January 31, 2020."
    llm = promo_llm({url: ([promo_item("Botox Bonus", line, end="2020-01-31")],
                           promo_review(("Botox Bonus", [line])))})
    plan = plan_for([page(url)], [scrape(url, f"Botox Bonus\n{line}", "Glow Clinic Botox Promo")], llm, tmp_path)
    row = entry_for(plan, action="rejected")
    assert row["reason"] == "expired_campaign"
    assert row["campaign_end_date"] == "2020-01-31"


def test_unknown_dates_are_kept_as_null(tmp_path):
    url = "https://glow.example/promo"
    plan = plan_for([page(url)], [scrape(url, BOTOX, "Glow Clinic Botox Promo")], promo_llm({url: BOTOX_PAGE}), tmp_path)
    row = entry_for(plan, action="insert")
    assert row["campaign_start_date"] is None and row["campaign_end_date"] is None


def test_price_list_page_without_discount_is_rejected(tmp_path):
    url = "https://glow.example/pricing"
    llm = promo_llm({url: ([promo_item("Botox Pricing", "Botox $12 per unit")],
                           promo_review(("Botox Pricing", ["Botox $12 per unit"])))})
    plan = plan_for([page(url)], [scrape(url, "Botox Pricing\nBotox $12 per unit", "Glow Clinic Pricing List")],
                    llm, tmp_path)
    assert entry_for(plan, action="rejected")["reason"] == "price_page_without_discount"


def test_page_reviewed_as_price_list_is_not_a_promotion(tmp_path):
    url = "https://glow.example/pricing"
    llm = promo_llm({url: ([promo_item("Botox Pricing", "Botox $12 per unit")],
                           promo_review(page_type="service_price_list"))})
    plan = plan_for([page(url)], [scrape(url, "Botox Pricing\nBotox $12 per unit", "Glow Clinic Pricing List")],
                    llm, tmp_path)
    assert entry_for(plan, action="rejected")["reason"] == "source_page_not_promotion"


def test_title_comes_from_scrape_metadata_not_llm(tmp_path):
    url = "https://glow.example/promo"
    llm = promo_llm({url: ([promo_item("LLM Invented Title", "Save 20% on Botox.")],
                           promo_review(("LLM Invented Title", ["Save 20% on Botox."])))})
    md = "LLM Invented Title\nSave 20% on Botox."
    plan = plan_for([page(url)], [scrape(url, md, "Glow Clinic | Botox Deals Page")], llm, tmp_path)
    assert entry_for(plan, action="insert")["title"] == "Glow Clinic | Botox Deals Page"


def test_page_without_usable_title_is_rejected(tmp_path):
    url = "https://glow.example/promo"
    plan = plan_for([page(url)], [scrape(url, BOTOX, "Home")], promo_llm({url: BOTOX_PAGE}), tmp_path)
    assert entry_for(plan, action="rejected")["reason"] == "no_usable_title"


def test_hub_title_fetch_failure_rejects_group(tmp_path):
    a = "https://glow.example/specials/botox"

    def fetch_title(url, name):
        raise TitleUnavailable("http_404")

    plan = plan_for([page(a)], [scrape(a, BOTOX, "child")], promo_llm({a: BOTOX_PAGE}), tmp_path,
                    fetch_title=fetch_title)
    assert entry_for(plan, action="rejected")["reason"] == "http_404"


def test_missing_scrape_and_llm_cache(tmp_path):
    url = "https://glow.example/promo"
    scrapes = [scrape(url, BOTOX, "Glow Clinic Botox Promo")]
    llm = promo_llm({url: BOTOX_PAGE})
    missing = "https://glow.example/other"
    first = plan_for([page(url), page(missing)], scrapes, llm, tmp_path)
    assert entry_for(first, source_url=missing)["reason"] == "no_scrape"
    assert llm.calls == 2  # 来源复核 + 抽取
    plan_for([page(url)], scrapes, llm, tmp_path)
    assert llm.calls == 2


def test_existing_rows_decide_update_and_unchanged(tmp_path):
    url = "https://glow.example/promo"
    scrapes = [scrape(url, BOTOX, "Glow Clinic Botox Promo")]
    llm = promo_llm({url: BOTOX_PAGE})
    key = (BUSINESS, "glow.example/promo")
    old = {"promotion_id": 7, "promotion_title": "Glow Clinic Botox Promo", "promotion_content": [BOTOX],
           "campaign_start_date": None, "campaign_end_date": None, "is_active": True, "updated_at": UPDATED_AT}
    same = plan_for([page(url)], scrapes, llm, tmp_path, state={**EMPTY_STATE, "promotions": {key: old}})
    assert entry_for(same, kind="promotion")["action"] == "unchanged"
    changed = {**old, "promotion_content": ["Botox Bonus"]}
    diff = plan_for([page(url)], scrapes, llm, tmp_path, state={**EMPTY_STATE, "promotions": {key: changed}})
    row = entry_for(diff, kind="promotion")
    assert (row["action"], row["promotion_id"], row["content_changed"]) == ("update", 7, True)


def visible(*cards: str) -> dict:
    return {"status": "page_accessible",
            "claimResults": [{"recordType": "promotion", "claim": card, "status": "visible_match"} for card in cards]}


def test_visibility_manifest_accepts_whole_visible_cards_and_rejects_invisible_pages(tmp_path):
    url = "https://glow.example/promo"
    scrapes = [scrape(url, BOTOX, "Glow Clinic Botox Promo")]
    llm = promo_llm({url: BOTOX_PAGE})
    plan = plan_for([page(url)], scrapes, llm, tmp_path, manifest={url: visible(BOTOX)})
    assert plan["visibility_manifest_sha256"] is not None
    assert entry_for(plan, action="insert")["content"] == [BOTOX]
    rejected = plan_for([page(url)], scrapes, llm, tmp_path, manifest={})
    assert entry_for(rejected, action="rejected")["reason"] == "not_in_visibility_manifest"
    # 只有卡片中的一行可见不能证明整张卡片可见
    partial = plan_for([page(url)], scrapes, llm, tmp_path, manifest={url: visible("Save 20% on Botox.")})
    assert entry_for(partial, action="rejected")["reason"] == "no_rendered_visible_promotion_content"


def test_one_invisible_card_sends_the_page_to_review(tmp_path):
    url = "https://glow.example/promo"
    markdown = f"{BOTOX}\nFiller Friday\nSave $100 on filler."
    llm = promo_llm({url: ([promo_item("Botox Bonus", "Save 20% on Botox."), promo_item("Filler Friday", "Save $100 on filler.")],
                           promo_review(("Botox Bonus", ["Save 20% on Botox."]), ("Filler Friday", ["Save $100 on filler."])))})
    plan = plan_for([page(url)], [scrape(url, markdown, "Glow Clinic Botox Promo")], llm, tmp_path,
                    manifest={url: visible(BOTOX)})
    assert entry_for(plan, kind="promotion")["reason"] == "promotion_card_not_fully_visible"


def membership_item(name: str, price: float) -> dict:
    return {"membership_name": name, "membership_price": price, "billing_period": "monthly",
            "minimum_commitment_months": None, "membership_description": None, "benefits": None}


def membership_llm(url: str, items: list[dict], review: dict) -> FakeLLM:
    return FakeLLM({url: {"explanation": "x", "memberships": items}}, {url: review})


def test_membership_identity_merges_case_variants_and_rejects_conflicts(tmp_path):
    url = "https://glow.example/membership/"
    md = "Glow Club $99 per month\nGlow Plus $150 per month, or $175 per month\n"
    items = [membership_item("Glow Club", 99), membership_item("glow club", 99), membership_item("Glow Plus", 150),
             membership_item("GLOW PLUS", 175)]
    review = membership_review(("Glow Club", ["Glow Club $99 per month"]),
                               ("Glow Plus", ["Glow Plus $150 per month, or $175 per month"]))
    plan = plan_for([page(url, "membership")], [scrape(url, md, "Members")], membership_llm(url, items, review), tmp_path)
    inserted = [e for e in plan["entries"] if e["action"] == "insert"]
    assert [e["membership_name"] for e in inserted] == ["Glow Club"]
    assert inserted[0]["source_url"] == "https://glow.example/membership"
    assert inserted[0]["target_key"] == {"business_id": BUSINESS, "source_url_key": "glow.example/membership",
                                          "membership_name_key": "glow club"}
    conflicts = [e for e in plan["entries"] if e["reason"] == "conflicting_duplicate_membership"]
    assert sorted(e["membership_price"] for e in conflicts) == [150, 175]


def test_duplicate_plan_name_in_source_inventory_requires_review(tmp_path):
    url = "https://glow.example/membership"
    md = "VIP\n$99 per month\nVIP\n$999 per year"
    review = membership_review(("VIP", ["$99 per month"]), ("VIP", ["$999 per year"]))
    plan = plan_for([page(url, "membership")], [scrape(url, md, "Members")],
                    membership_llm(url, [membership_item("VIP", 99)], review), tmp_path)
    row = entry_for(plan, kind="membership")
    assert (row["action"], row["reason"]) == ("rejected", "ambiguous_membership_source_inventory")


def test_membership_missing_from_source_inventory_is_written_without_benefits(tmp_path):
    url = "https://glow.example/membership"
    md = "VIP Membership $199 per month\nOne facial monthly"
    item = {**membership_item("VIP Membership", 199), "benefits": ["One facial monthly"]}
    review = membership_review(("VIP", ["One facial monthly"]))
    plan = plan_for([page(url, "membership")], [scrape(url, md, "Members")],
                    membership_llm(url, [item], review), tmp_path)
    row = entry_for(plan, kind="membership")
    assert (row["action"], row["membership_price"], row["benefits"]) == ("insert", 199, None)
    assert row["benefits_review_reason"] == "membership_missing_from_source_inventory"


def test_paraphrased_membership_claim_makes_the_benefit_inventory_incomplete(tmp_path):
    url = "https://glow.example/membership"
    md = "VIP $199 per month\nOne facial monthly"
    item = {**membership_item("VIP", 199), "benefits": ["One facial monthly"]}
    review = membership_review(("VIP", ["One facial monthly", "Two free peels"]))
    manifest = {url: {"status": "page_accessible", "complete": True, "claimResults": [
        {"recordType": "membership", "claim": claim, "status": "visible_match"}
        for claim in ("VIP", "One facial monthly")]}}
    plan = plan_for([page(url, "membership")], [scrape(url, md, "Members")], membership_llm(url, [item], review),
                    tmp_path, manifest=manifest)
    row = entry_for(plan, kind="membership")
    # 复核中有一条权益不在页面上：清单不可信，会员照常写入费用，权益不判定为完整
    assert (row["action"], row["membership_price"], row["benefits"]) == ("insert", 199, None)
    assert row["benefits_review_reason"] == "membership_benefits_unverified"
    assert row["source_review"]["review"]["unverified_quotes"] == [
        {"membership_claims": {"name": "VIP", "claims": ["Two free peels"]}}]
    # 复核清单完整时同一页面的权益判定为完整
    complete = membership_review(("VIP", ["One facial monthly"]))
    verified = plan_for([page(url, "membership")], [scrape(url, md, "Members", fingerprint="c")],
                        membership_llm(url, [item], complete), tmp_path, manifest=manifest)
    assert entry_for(verified, kind="membership")["benefits"] == ["One facial monthly"]


def test_membership_fee_not_supported_is_rejected(tmp_path):
    url = "https://glow.example/membership"
    review = membership_review(("Glow Club", []))
    plan = plan_for([page(url, "membership")], [scrape(url, "Glow Club\nno prices here", "Members")],
                    membership_llm(url, [membership_item("Glow Club", 99)], review), tmp_path)
    assert entry_for(plan, action="rejected")["reason"] == "membership_fee_not_supported_by_named_evidence"


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
    mvisible = {"status": "page_accessible", "claimResults": []}
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


def test_hub_page_in_batch_keeps_hub_title(tmp_path):
    hub = "https://glow.example/specials"
    child = f"{hub}/botox"
    llm = promo_llm({child: BOTOX_PAGE,
                     hub: ([promo_item("Fall Specials", "Save 20% on fillers.")],
                           promo_review(("Fall Specials", ["Save 20% on fillers."])))})
    scrapes = [scrape(hub, "Fall Specials\nSave 20% on fillers.", "Specials | Glow Clinic"),
               scrape(child, BOTOX, "Botox child title")]
    row = entry_for(plan_for([page(hub), page(child)], scrapes, llm, tmp_path), action="insert")
    assert (row["source_url"], row["title"]) == (hub, "Specials | Glow Clinic")
    assert row["source_pages"] == [hub, child]


HUB = "https://glow.example/specials"
HUB_KEY = (BUSINESS, "glow.example/specials")


def hub_state(*content: str) -> dict:
    old = {"promotion_id": 9, "promotion_title": "Specials | Glow Clinic", "promotion_content": list(content),
           "campaign_start_date": None, "campaign_end_date": None, "is_active": True, "updated_at": UPDATED_AT}
    return {**EMPTY_STATE, "promotions": {HUB_KEY: old}}


def test_expired_child_is_not_merged_into_hub_and_its_stored_card_is_removed(tmp_path):
    old_child, live_child = f"{HUB}/valentines", f"{HUB}/botox"
    line = "Save 30% through February 14, 2020."
    llm = promo_llm({old_child: ([promo_item("Valentine Glow", line, end="2020-02-14")],
                                 promo_review(("Valentine Glow", [line]))),
                     live_child: BOTOX_PAGE})
    scrapes = [scrape(old_child, f"Valentine Glow\n{line}", "Valentine"), scrape(live_child, BOTOX, "Botox"),
               scrape(HUB, "links", "Specials | Glow Clinic")]
    plan = plan_for([page(old_child), page(live_child)], scrapes, llm, tmp_path,
                    state=hub_state(f"Valentine Glow\n{line}"))
    assert entry_for(plan, source_url=old_child)["reason"] == "expired_campaign"
    row = entry_for(plan, kind="promotion", action="update")
    assert row["content"] == [BOTOX] and row["source_pages"] == [live_child]


def test_partial_hub_batch_keeps_cards_of_children_outside_the_batch(tmp_path):
    child = f"{HUB}/botox"
    scrapes = [scrape(child, BOTOX, "Botox"), scrape(HUB, "links", "Specials | Glow Clinic")]
    state = hub_state("Filler Friday", "Save $100 on filler.")
    row = entry_for(plan_for([page(child)], scrapes, promo_llm({child: BOTOX_PAGE}), tmp_path, state=state),
                    kind="promotion")
    assert row["action"] == "update"
    assert row["content"] == ["Filler Friday", "Save $100 on filler.", BOTOX]


def test_recrawled_child_replaces_its_changed_card_in_the_hub(tmp_path):
    child = f"{HUB}/botox"
    llm = promo_llm({child: ([promo_item("Botox Bonus", "Botox $11/unit")], promo_review(("Botox Bonus", ["Botox $11/unit"])))})
    scrapes = [scrape(child, "Botox Bonus\nBotox $11/unit", "Botox"), scrape(HUB, "links", "Specials | Glow Clinic")]
    state = hub_state("Botox Bonus\nBotox $12/unit", "Filler Friday\nSave $100 on filler.")
    row = entry_for(plan_for([page(child)], scrapes, llm, tmp_path, state=state), kind="promotion")
    assert row["content"] == ["Filler Friday\nSave $100 on filler.", "Botox Bonus\nBotox $11/unit"]


def test_recrawled_child_card_with_another_rendering_replaces_the_stored_card(tmp_path):
    child = f"{HUB}/botox"
    llm = promo_llm({child: ([promo_item("Botox", "$15", "$12")], promo_review(("Botox", ["$15", "$12"])))})
    scrapes = [scrape(child, "Botox\n~~$15~~\n$12", "Botox"), scrape(HUB, "links", "Specials | Glow Clinic")]
    state = hub_state("Botox\nRegular price $15\n$12")
    row = entry_for(plan_for([page(child)], scrapes, llm, tmp_path, state=state), kind="promotion")
    assert row["content"] == ["Botox\n~~$15~~\n$12"]


def test_hub_page_listing_child_titles_keeps_those_children_cards(tmp_path):
    llm = promo_llm({HUB: ([promo_item("Fall Specials", "Save 20% on fillers.")],
                           promo_review(("Fall Specials", ["Save 20% on fillers."])))})
    scrapes = [scrape(HUB, "Fall Specials\nSave 20% on fillers.\nFiller Friday", "Specials | Glow Clinic")]
    state = hub_state("Filler Friday\nSave $100 on filler.", "Fall Specials\nSave 10% on fillers.")
    row = entry_for(plan_for([page(HUB)], scrapes, llm, tmp_path, state=state), kind="promotion")
    assert row["content"] == ["Filler Friday\nSave $100 on filler.", "Fall Specials\nSave 20% on fillers."]


def test_markdown_variants_of_one_line_are_merged_within_a_block():
    block = ["* **Buy 2 Radiesse, get the 3rd half off** (save $475).",
             "**Buy 2 Radiesse, get the 3rd half off** (save $475).",
             "Regularly $3,500", "**Regularly $3,500**"]
    assert merge_content_blocks([block]) == ["Buy 2 Radiesse, get the 3rd half off (save $475).\nRegularly $3,500"]


def test_repeated_block_is_dropped_but_shared_short_lines_stay_with_each_offer():
    blocks = [["Forehead TOX", "Regular price", "$69.00", "~~$135.00~~"],
              ["Jelly Roll", "Regular price", "$99.00", "~~$135.00~~"],
              ["**Forehead TOX**", "Regular price", "$69.00", "~~$135.00~~"]]
    assert merge_content_blocks(blocks) == ["Forehead TOX\nRegular price\n$69.00\n~~$135.00~~",
                                            "Jelly Roll\nRegular price\n$99.00\n~~$135.00~~"]


def test_specials_title_with_business_name_is_accepted(tmp_path):
    plan = plan_for([page(HUB)], [scrape(HUB, BOTOX, "Specials | Glow Clinic")], promo_llm({HUB: BOTOX_PAGE}), tmp_path)
    assert entry_for(plan, action="insert")["title"] == "Specials | Glow Clinic"
