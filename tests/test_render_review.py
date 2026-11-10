from __future__ import annotations

from utils.render_review import claim_anchors, normalize_anchor, shard


def test_anchors_are_normalised_prices_and_percents() -> None:
    line = "Regular price ~$1,800.00 USD~ Sale price $799.00 USD, save 15 % today, Botox $12.50/unit"
    assert claim_anchors(line) == ["$1800", "$799", "$12.50", "15%"]
    assert normalize_anchor("$ 420.00") == "$420"
    assert claim_anchors("Ask us about our specials") == []


def test_shards_are_balanced() -> None:
    items = [{"id": str(i)} for i in range(11)]
    parts = shard(items, 4)
    assert sorted(len(p) for p in parts) == [2, 3, 3, 3] and sum(len(p) for p in parts) == 11
    assert len(shard([], 3)) == 0


def test_pages_waiting_for_the_render_are_queued_once_per_url() -> None:
    from utils.render_review import build_deep_items

    decisions = [
        {"url": "https://laseraway.com/promotions", "business_id": 4385, "status": "needs_review",
         "decision": "add_with_review", "evidence": ["Your First Botox Treatment Is $10 Per Unit"]},
        {"url": "https://laseraway.com/promotions", "business_id": 4386, "status": "needs_review",
         "decision": "add_with_review", "evidence": ["Your First Botox Treatment Is $10 Per Unit"]},
        # 上次渲染被拦截、仍在等待的页面再次进入队列
        {"url": "https://spa.com/specials", "business_id": 1, "status": "needs_review", "decision": "add_with_review",
         "render_review_deep": {"verdict": "page_unavailable"}, "evidence": ["Botox $11 per unit"]},
        {"url": "https://spa.com/deals", "business_id": 1, "status": "needs_review", "decision": "add",
         "render_review_deep": {"verdict": "verified"}, "evidence": ["Filler $499"]},
        {"url": "https://spa.com/hidden", "business_id": 1, "status": "needs_review", "decision": "skip",
         "evidence": ["Filler $399"]},
        {"url": "https://spa.com/offers", "business_id": 1, "status": "valid", "decision": "add_with_review",
         "evidence": ["Peel $99"]},
    ]
    assert [i["url"] for i in build_deep_items(decisions)] == ["https://laseraway.com/promotions",
                                                               "https://spa.com/specials"]


def test_deep_verdict_uses_expanded_text_and_image_text() -> None:
    from utils.render_review import deep_verdict

    claims = [{"line": "Botox $11/unit for members", "anchors": ["$11"]},
              {"line": "[Skip to content](#main)", "anchors": []}]
    assert deep_verdict(claims, ["Botox", "$11 / unit", "members only"])["verdict"] == "verified"
    assert deep_verdict(claims, ["Contact us"])["verdict"] == "not_visible"
    from_image = deep_verdict(claims, ["Contact us"], ["BOTOX $11 per unit"])
    assert from_image["verdict"] == "verified" and from_image["source"] == "image_text"


def test_deep_verdict_matches_text_claims_without_numbers() -> None:
    from utils.render_review import deep_verdict

    claims = [{"line": "Buy 2 Body Sculpting Treatments & Get 2 Complimentary Skin Tightening Sessions", "anchors": []}]
    page = ["Buy 2 body sculpting treatments and get 2 complimentary skin tightening sessions"]
    assert deep_verdict(claims, page)["verdict"] == "verified"
    assert deep_verdict(claims, ["Our team of injectors"])["verdict"] == "not_visible"
