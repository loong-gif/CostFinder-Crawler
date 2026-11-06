from __future__ import annotations

from page_extraction_support import (
    BOTOX,
    BOTOX_PAGE,
    HUB,
    entry_for,
    hub_state,
    page,
    plan_for,
    promo_item,
    promo_llm,
    promo_review,
    scrape,
)
from utils.page_title import TitleUnavailable


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


def test_hub_title_fetch_failure_rejects_group(tmp_path):
    a = "https://glow.example/specials/botox"

    def fetch_title(url, name):
        raise TitleUnavailable("http_404")

    plan = plan_for([page(a)], [scrape(a, BOTOX, "child")], promo_llm({a: BOTOX_PAGE}), tmp_path,
                    fetch_title=fetch_title)
    assert entry_for(plan, action="rejected")["reason"] == "http_404"


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
    # hub 页面上列出的子页面卡片保留；全部卡片按页面上的出现顺序排列
    assert row["content"] == ["Fall Specials\nSave 20% on fillers.", "Filler Friday\nSave $100 on filler."]


def test_specials_title_with_business_name_is_accepted(tmp_path):
    plan = plan_for([page(HUB)], [scrape(HUB, BOTOX, "Specials | Glow Clinic")], promo_llm({HUB: BOTOX_PAGE}), tmp_path)
    assert entry_for(plan, action="insert")["title"] == "Specials | Glow Clinic"
