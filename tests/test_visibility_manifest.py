from __future__ import annotations

from utils.visibility_manifest import build_manifest, build_shard

PLAN = {"entries": [
    {"kind": "promotion", "action": "insert", "source_url": "https://a.com/specials",
     "source_pages": ["https://a.com/specials"], "content": ["Botox $10/unit this month", "Free consultation for Radiesse"]},
    {"kind": "promotion", "action": "update", "source_url": "https://b.com/specials", "content": ["Lip filler $650"]},
    {"kind": "promotion", "action": "insert", "source_url": "https://c.com/specials", "content": []},
    {"kind": "promotion", "action": "rejected", "source_url": "https://d.com/specials", "content": ["Botox $9/unit"],
     "reason": "price_page_without_discount"},
    {"kind": "membership", "action": "rejected", "source_url": "https://e.com/membership",
     "reason": "source_page_not_membership"},
]}


def test_shard_has_one_item_per_page_with_content():
    shard = build_shard(PLAN, "run")
    assert [i["url"] for i in shard["items"]] == ["https://a.com/specials", "https://b.com/specials"]
    assert [c["line"] for c in shard["items"][0]["claims"]] == ["Botox $10/unit this month",
                                                                 "Free consultation for Radiesse"]


def test_plan_whose_fee_needs_the_render_layout_is_rendered():
    # 费用 "$99/month" 与方案名称不在同一行，只有渲染布局能证实费用属于该方案
    plan = {"entries": [{"kind": "membership", "action": "rejected", "source_url": "https://f.com/memberships",
                         "reason": "membership_fee_not_supported_by_named_evidence", "membership_name": "Essence",
                         "membership_price": 99.0, "source_claims": ["$99/month", "40 Botox units"]}]}
    shard = build_shard(plan, "run")
    assert [(c["line"], c["is_name"]) for c in shard["items"][0]["claims"]] == [
        ("Essence", True), ("Essence $99", True), ("$99/month", False), ("40 Botox units", False)]


# 渲染后可见文本：真实页面的导航、正文与页脚，长度足以判断页面已正常渲染
PAGE_CHROME = ["Home", "About Us", "Services", "Book Now", "Contact",
               "Our board-certified injectors have served the community for over ten years with natural results.",
               "Call us today to schedule a complimentary consultation with one of our aesthetic specialists.",
               "Hours: Monday to Friday 9am - 6pm, Saturday 10am - 4pm. Located downtown near the main square."]


def test_manifest_marks_visible_lines_and_unrendered_pages():
    shard = build_shard(PLAN, "run")
    render = {"results": [{"url": "https://a.com/specials", "checked_at": "t",
                           "result": {"texts": [*PAGE_CHROME, "Specials", "Botox $10/unit this month"]}},
                          {"url": "https://b.com/specials", "error": "timeout"}]}
    manifest = build_manifest(shard, render)
    statuses = [c["status"] for c in manifest["https://a.com/specials"]["claimResults"]]
    assert statuses == ["visible_match", "not_visible"]
    assert manifest["https://b.com/specials"] == {"status": "page_not_accessible", "reason": "timeout",
                                                  "claimResults": []}


def test_render_answered_with_404_marks_the_page_gone():
    render = {"results": [{"url": "https://a.com/specials", "checked_at": "t", "http_status": 404,
                           "result": {"texts": ["404 Not Found"]}}]}
    page = build_manifest(build_shard(PLAN, "run"), render)["https://a.com/specials"]
    assert (page["status"], page["reason"]) == ("page_not_accessible", "page_gone")


def test_blank_render_is_page_unavailable_not_invisible_claims():
    shard = build_shard(PLAN, "run")
    render = {"results": [{"url": "https://a.com/specials", "checked_at": "t",
                           "result": {"texts": ["a.com"]}}]}
    page = build_manifest(shard, render)["https://a.com/specials"]
    assert (page["status"], page["reason"], page["claimResults"]) == (
        "page_not_accessible", "blank_or_blocked_render", [])


def _statuses(lines: list[str], texts: list[str]) -> list[str]:
    plan = {"entries": [{"kind": "membership", "action": "insert", "source_url": "https://m.com/membership",
                         "membership_name": lines[0], "benefits": lines[1:]}]}
    render = {"results": [{"url": "https://m.com/membership", "checked_at": "t",
                           "result": {"texts": [*PAGE_CHROME, *texts]}}]}
    return [c["status"] for c in build_manifest(build_shard(plan, "run"), render)["https://m.com/membership"]["claimResults"]]


def test_rephrased_benefits_are_traceable_on_the_page():
    texts = ["Glow Club", "$99/month", "Monthly billing with automatic renewal. Six month minimum commitment.",
             "50 units every 4 months", "Wellness shots must be used within the same month and do not roll over."]
    lines = ["Glow Club", "Six-month minimum commitment with automatic monthly renewal.",
             "50 Botox units every four months", "Wellness shots do not roll over and must be used the same month."]
    assert _statuses(lines, texts) == ["visible_match"] * 4


def test_number_from_another_offer_does_not_make_a_claim_visible():
    texts = ["Glow Club", "20% off all DiamondGlow Facials", "Members enjoy priority booking.",
             "Silver Membership $129 per month"]
    lines = ["Glow Club", "20% off skincare products", "Silver Membership $99", "Morpheus8 requires an additional $599"]
    assert _statuses(lines, texts) == ["visible_match", "not_visible", "not_visible", "not_visible"]


def test_common_wordings_and_facts_from_separate_places_are_traceable():
    texts = ["Glow Club", "50% off", "Ends Nov 30", "Join for $149/month",
             *["Our providers tailor every plan to your goals and skin type."] * 6,
             "The full monthly fee is banked into your treatment account.",
             "Twice a year: 10% off any Filler/Skinvive/Sculptra treatment",
             "Once complimentary Microneedling session per year"]
    lines = ["Glow Club", "Now 50% off through November 30",
             "Full $149 monthly fee is banked into your treatment account.",
             "Twice annually, 10% off filler/SkinVive/Sculptra; one complimentary microneedling session per year."]
    assert _statuses(lines, texts) == ["visible_match"] * 4


def _name_statuses(names: list[str], texts: list[str]) -> dict[str, str]:
    plan = {"entries": [{"kind": "membership", "action": "insert", "source_url": "https://m.com/membership",
                         "membership_name": name, "benefits": []} for name in names]}
    render = {"results": [{"url": "https://m.com/membership", "checked_at": "t",
                           "result": {"texts": [*PAGE_CHROME, *texts]}}]}
    results = build_manifest(build_shard(plan, "run"), render)["https://m.com/membership"]["claimResults"]
    return {c["claim"]: c["status"] for c in results}


def test_plan_name_words_scattered_over_the_page_do_not_match_the_name():
    texts = ["Bold Confidence Aesthetics is dedicated to services that elevate your natural beauty.",
             "Bold Basics", "$99/month"]
    assert _name_statuses(["Bold Elevated", "Bold Basics"], texts) == {"Bold Elevated": "not_visible",
                                                                     "Bold Basics": "visible_match"}


def test_plan_price_a_line_after_the_name_belongs_to_the_name():
    texts = ["Glow Club", "Peptide Membership", "Skin repair, recovery, fat loss, sleep & longevity", "$199 / month",
             "Founders Glow Getter Club", "$99.00 / month"]
    plan = {"entries": [
        {"kind": "membership", "action": "insert", "source_url": "https://m.com/membership", "membership_name": name,
         "source_claims": [f"{name} {price}"], "benefits": []}
        for name, price in (("Peptide Membership", "$199"), ("Founders Beauty Bank", "$99"))]}
    render = {"results": [{"url": "https://m.com/membership", "checked_at": "t",
                           "result": {"texts": [*PAGE_CHROME, *texts]}}]}
    results = build_manifest(build_shard(plan, "run"), render)["https://m.com/membership"]["claimResults"]
    assert {c["claim"]: c["status"] for c in results} == {
        "Peptide Membership": "visible_match", "Peptide Membership $199": "visible_match",
        "Founders Beauty Bank": "not_visible", "Founders Beauty Bank $99": "not_visible"}


def test_quantity_missing_from_the_page_is_not_visible():
    texts = ["Glow Club", "50 units every 4 months", "100 units every 4 months",
             "Unused facials roll over for 60 days", "Credits remain available after cancellation until expiration"]
    lines = ["Glow Club", "75 Botox units every four months", "100 Botox units every four months",
             "Banked credits expire 24 months after issue and remain available after cancellation"]
    assert _statuses(lines, texts) == ["visible_match", "not_visible", "visible_match", "not_visible"]
