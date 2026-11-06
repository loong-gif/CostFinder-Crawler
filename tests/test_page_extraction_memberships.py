from __future__ import annotations

from page_extraction_support import (
    BUSINESS,
    FakeLLM,
    entry_for,
    membership_item,
    membership_llm,
    membership_review,
    page,
    plan_for,
    promo_item,
    scrape,
)


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


def test_membership_page_with_promotion_cards_writes_only_memberships(tmp_path):
    url = "https://glow.example/memberships"
    md = "VIP Membership $199 per month\nBotox Bonus\nSave 20% on Botox."
    review = {**membership_review(("VIP Membership", [])), "page_type": "mixed",
              "promotion_quotes": [{"title": "Botox Bonus", "lines": ["Save 20% on Botox."]}]}
    llm = FakeLLM({url: {"explanation": "x", "memberships": [membership_item("VIP Membership", 199)],
                         "promotions": [promo_item("Botox Bonus", "Save 20% on Botox.")]}}, {url: review})
    plan = plan_for([page(url, "membership")], [scrape(url, md, "Members")], llm, tmp_path)
    assert [e["kind"] for e in plan["entries"]] == ["membership"]


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
        for claim in ("VIP", "VIP $199", "One facial monthly")]}}
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


def test_membership_name_or_fee_not_visible_in_the_render_is_rejected(tmp_path):
    url = "https://glow.example/membership"
    md = "Founders Beauty Bank $99 per month\nOne facial monthly"
    item = {**membership_item("Founders Beauty Bank", 99), "benefits": ["One facial monthly"]}
    review = membership_review(("Founders Beauty Bank", ["One facial monthly"]))
    # 渲染页面上只看到权益，方案名称与费用都不可见
    manifest = {url: {"status": "page_accessible", "complete": False, "claimResults": [
        {"recordType": "membership", "claim": "One facial monthly", "status": "visible_match"}]}}
    plan = plan_for([page(url, "membership")], [scrape(url, md, "Members")], membership_llm(url, [item], review),
                    tmp_path, manifest=manifest)
    row = entry_for(plan, kind="membership")
    assert (row["action"], row["reason"]) == ("rejected", "membership_name_or_fee_not_visible")


def test_rephrased_benefit_is_kept_and_a_benefit_with_a_price_not_on_the_page_is_dropped(tmp_path):
    from utils.membership_evidence import prepare_membership_write

    evidence = "VIP $199 per month\nOne complimentary facial each month\n20% off all skincare"
    item = {"membership_name": "VIP", "membership_price": 199,
            "benefits": ["One free facial every month", "20% off skincare products", "$50 off any filler"]}
    rendered = {"status": "page_accessible", "claimResults": [
        {"recordType": "membership", "claim": claim, "status": "visible_match"} for claim in item["benefits"]]}
    decision = prepare_membership_write(item, evidence, rendered)
    assert decision["accepted"] and decision["verified_benefits"] == ["One free facial every month",
                                                                      "20% off skincare products"]


def test_membership_fee_not_supported_is_rejected(tmp_path):
    url = "https://glow.example/membership"
    review = membership_review(("Glow Club", []))
    plan = plan_for([page(url, "membership")], [scrape(url, "Glow Club\nno prices here", "Members")],
                    membership_llm(url, [membership_item("Glow Club", 99)], review), tmp_path)
    assert entry_for(plan, action="rejected")["reason"] == "membership_fee_not_supported_by_named_evidence"
