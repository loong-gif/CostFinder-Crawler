from __future__ import annotations

from page_extraction_support import (
    BOTOX,
    BOTOX_PAGE,
    FakeLLM,
    NAMES,
    EMPTY_STATE,
    entry_for,
    page,
    plan_for,
    promo_item,
    promo_llm,
    promo_review,
    scrape,
)
from utils.promo_extraction import merge_content_blocks


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


def test_page_reviewed_as_price_list_is_not_a_promotion(tmp_path):
    url = "https://glow.example/pricing"
    llm = promo_llm({url: ([promo_item("Botox Pricing", "Botox $12 per unit")],
                           promo_review(page_type="service_price_list"))})
    plan = plan_for([page(url)], [scrape(url, "Botox Pricing\nBotox $12 per unit", "Glow Clinic Pricing List")],
                    llm, tmp_path)
    assert entry_for(plan, action="rejected")["reason"] == "source_page_not_promotion"


def test_link_residue_is_not_stored_as_promotion_content():
    assert merge_content_blocks([["! Juvederm $75 off 1st syringe (https://oubeauty.com/juvederm-special/)",
                                  "Save now https://oubeauty.com/book"]]) == ["Juvederm $75 off 1st syringe\nSave now"]


def test_source_quotes_with_rewritten_footnote_marks_still_count_as_verbatim():
    from utils.page_extraction_source_review import _title_key, verified_source_review
    from utils.promo_extraction import comparison_key

    page_text = ("### 40% Off Your First Clear + Brilliant Laser Facial✢\n\nImprove texture and tone.\n\n"
                 "### Your First Xeomin Treatment: $7 Per Unit^^\n\nAnother popular injection.\n\n"
                 "### 50% Off Your First Laser Tattoo Removal Treatmentⱽ\n\nRemove unwanted ink.")
    review = {"page_type": "promotion", "memberships": [], "promotion_quotes": [
        {"title": "40% Off Your First Clear + Brilliant Laser Facial❢", "lines": ["Improve texture and tone."]},
        {"title": "Your First Xeomin Treatment: $7 Per Unit\\u02c4\\u02c4", "lines": ["Another popular injection."]},
        {"title": "50% Off Your First Laser Tattoo Removal Treatment⁵", "lines": ["Remove unwanted ink."]},
        # 文字或金额不同仍然不是原文
        {"title": "Your First Xeomin Treatment: $6 Per Unit", "lines": ["Another popular injection."]},
    ]}
    verified = verified_source_review(review, comparison_key(page_text))
    assert [q["title"] for q in verified["promotion_quotes"]] == [
        "40% Off Your First Clear + Brilliant Laser Facial❢", "Your First Xeomin Treatment: $7 Per Unit˄˄",
        "50% Off Your First Laser Tattoo Removal Treatment⁵"]
    assert len(verified["unverified_quotes"]) == 1
    assert _title_key("40% Off Your First Clear + Brilliant Laser Facial✢") == \
        _title_key("40% Off Your First Clear + Brilliant Laser Facial❢")


def test_terms_pointer_split_or_joined_by_the_llm_gives_the_same_card():
    split = merge_content_blocks([["The Black Friday Sale", "75% OFF LASER HAIR REMOVALǂ",
                                   "Select packages. Subject to", "Offer Terms"]])
    joined = merge_content_blocks([["The Black Friday Sale", "75% OFF LASER HAIR REMOVAL",
                                    "Select packages. Subject to Offer Terms"]])
    assert split == joined == ["The Black Friday Sale\n75% OFF LASER HAIR REMOVAL\nSelect packages."]


def test_cards_follow_page_order_whatever_order_the_llm_returns(tmp_path):
    url = "https://glow.example/specials"
    text = "# Specials\n\n### Botox Bonus\n\nSave 20% on Botox.\n\n### Filler Friday\n\nSave $100 on filler."
    cards = (("Botox Bonus", ["Save 20% on Botox."]), ("Filler Friday", ["Save $100 on filler."]))
    contents = []
    for order in (cards, cards[::-1]):
        llm = FakeLLM({url: {"explanation": "", "promotions": [promo_item(t, *lines) for t, lines in order]}},
                      {url: promo_review(*order)})
        plan = plan_for([page(url)], [scrape(url, text, "Glow Specials")], llm, tmp_path / str(len(contents)))
        contents.append(entry_for(plan, action="insert")["content"])
    assert contents[0] == contents[1] == ["Botox Bonus\nSave 20% on Botox.", "Filler Friday\nSave $100 on filler."]


def test_only_the_promotion_hub_page_asks_for_site_wide_banner_offers(tmp_path):
    from utils.page_extraction_source_review import HUB_BANNER_NOTE

    for url, expected in (("https://laseraway.com/promotions", True), ("https://glow.example/botox", False)):
        llm = promo_llm({url: BOTOX_PAGE})
        plan_for([page(url)], [scrape(url, BOTOX, "Glow Clinic Botox Promo")], llm, tmp_path / url.split("/")[2])
        assert llm.prompts and all((HUB_BANNER_NOTE in p) is expected for p in llm.prompts)


def test_footnote_marks_and_call_to_action_lines_are_normalised_away():
    # LLM 时保留时去掉脚注符号与按钮文字，两种回答规范化后得到同一条内容
    kept = merge_content_blocks([["Your First Botox Treatment Is $10 Per Unit‡‡", "Book Here",
                                  "Our #1 wrinkle relaxer.", "Subject to offer terms",
                                  "50% Savings On CoolSculpting (8+ Treatments)\\*\\*", "Was ~~$135~~ now $99"]])
    dropped = merge_content_blocks([["Your First Botox Treatment Is $10 Per Unit", "Our #1 wrinkle relaxer.",
                                     "50% Savings On CoolSculpting (8+ Treatments)", "Was ~~$135~~ now $99"]])
    assert kept == dropped == ["Your First Botox Treatment Is $10 Per Unit\nOur #1 wrinkle relaxer.\n"
                               "50% Savings On CoolSculpting (8+ Treatments)\nWas ~~$135~~ now $99"]


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
