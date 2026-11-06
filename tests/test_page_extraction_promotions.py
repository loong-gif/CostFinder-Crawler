from __future__ import annotations

import pytest

from page_extraction_support import (
    BOTOX,
    BOTOX_PAGE,
    BUSINESS,
    EMPTY_STATE,
    UPDATED_AT,
    entry_for,
    page,
    plan_for,
    promo_item,
    promo_llm,
    promo_review,
    scrape,
    visible,
)


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
