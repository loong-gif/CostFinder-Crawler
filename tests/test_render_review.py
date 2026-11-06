from __future__ import annotations

from utils.render_review import (
    apply_verdicts, build_items, claim_anchors, item_id, normalize_anchor, page_verdict, shard,
)


def test_anchors_are_normalised_prices_and_percents() -> None:
    line = "Regular price ~$1,800.00 USD~ Sale price $799.00 USD, save 15 % today, Botox $12.50/unit"
    assert claim_anchors(line) == ["$1800", "$799", "$12.50", "15%"]
    assert normalize_anchor("$ 420.00") == "$420"
    assert claim_anchors("Ask us about our specials") == []


def _decision(url, status="needs_review", decision="add_with_review", evidence=("Botox $11 per unit",)):
    return {"url": url, "status": status, "decision": decision, "business_id": 1, "business": "S",
            "evidence": list(evidence), "reasons": []}


def test_queue_only_holds_kept_needs_review_pages_and_skips_finished_ones() -> None:
    decisions = [_decision("https://s.com/a"), _decision("https://s.com/a"),
                 _decision("https://s.com/b", status="valid"), _decision("https://s.com/c", decision="skip"),
                 _decision("https://s.com/d"), _decision("https://s.com/e")]
    done = {"https://s.com/d": {"verdict": "verified"}, "https://s.com/e": {"verdict": "page_unavailable"}}
    items = build_items(decisions, done)
    assert [i["url"] for i in items] == ["https://s.com/a", "https://s.com/e"]  # unavailable pages are retried
    assert items[0]["id"] == item_id("https://s.com/a") and items[0]["claims"][0]["anchors"] == ["$11"]


def test_shards_are_balanced() -> None:
    items = [{"id": str(i)} for i in range(11)]
    parts = shard(items, 4)
    assert sorted(len(p) for p in parts) == [2, 3, 3, 3] and sum(len(p) for p in parts) == 11
    assert len(shard([], 3)) == 0


def test_page_verdicts() -> None:
    claims = lambda *statuses: {"result": {"claims": [{"anchors": ["$1"], "status": s} for s in statuses]}}  # noqa: E731
    assert page_verdict(claims("visible_match", "visible_match"))["verdict"] == "verified"
    assert page_verdict(claims("visible_match", "not_found", "not_found"))["verdict"] == "not_visible"
    assert page_verdict(claims("dom_only_hidden"))["reason"] == "claims_only_in_hidden_elements"
    assert page_verdict(claims("not_found"))["reason"] == "claims_not_found_in_rendered_page"
    assert page_verdict({"error": "timeout"})["verdict"] == "page_unavailable"
    no_anchor = page_verdict({"result": {"claims": [{"anchors": [], "status": "no_anchor"}]}})
    assert no_anchor["verdict"] == "manual_review"  # nothing numeric to match is not evidence of invisibility


def test_apply_verdicts_promotes_verified_skips_hidden_and_keeps_unavailable() -> None:
    decisions = [_decision("https://s.com/a"), _decision("https://s.com/b"), _decision("https://s.com/c"),
                 _decision("https://s.com/d", status="valid", decision="add")]
    verdicts = {"https://s.com/a": {"verdict": "verified"}, "https://s.com/b": {"verdict": "not_visible"},
                "https://s.com/c": {"verdict": "page_unavailable"}, "https://s.com/d": {"verdict": "not_visible"},
                "https://s.com/e": {"verdict": "manual_review"}}
    decisions.append(_decision("https://s.com/e"))
    out = {d["url"]: d for d in apply_verdicts(decisions, verdicts)}
    assert out["https://s.com/a"]["decision"] == "add" and "render_verified" in out["https://s.com/a"]["reasons"]
    assert out["https://s.com/b"]["decision"] == "skip"
    assert out["https://s.com/c"]["decision"] == "add_with_review"
    assert out["https://s.com/d"]["decision"] == "add"  # valid pages are untouched by the render review
    assert out["https://s.com/e"]["decision"] == "add_with_review"  # manual review neither promotes nor drops


def test_unreviewed_and_unverified_pages_are_queued_for_the_deep_review_once() -> None:
    from utils.render_review import build_deep_items

    decisions = [
        {"url": "https://laseraway.com/promotions", "business_id": 4385, "status": "needs_review",
         "evidence": ["Your First Botox Treatment Is $10 Per Unit"]},
        {"url": "https://laseraway.com/promotions", "business_id": 4386, "status": "needs_review",
         "evidence": ["Your First Botox Treatment Is $10 Per Unit"]},
        {"url": "https://spa.com/specials", "business_id": 1, "status": "needs_review",
         "render_review": {"verdict": "not_visible"}, "evidence": ["Botox $11 per unit"]},
        {"url": "https://spa.com/deals", "business_id": 1, "status": "needs_review",
         "render_review": {"verdict": "verified"}, "evidence": ["Filler $499"]},
        {"url": "https://spa.com/offers", "business_id": 1, "status": "valid", "evidence": ["Peel $99"]},
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
