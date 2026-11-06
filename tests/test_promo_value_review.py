from __future__ import annotations

import datetime as dt
from utils.promo_value_review import BusinessContext, decide, review_page

TODAY = dt.date(2026, 10, 15)


def _biz(**kw):
    base = dict(business_id=1, name="Spa", city="Reno", review_count=120, score=4.8, website="https://spa.com",
                has_active_offer=False, never_processed=False)
    base.update(kw)
    return BusinessContext(**base)


def _page(**kw):
    base = dict(url="https://spa.com/specials", status="valid", kind="promotion", business_id=1,
                evidence=["Botox $11 per unit this month", "20% off laser hair removal"], sources=["map"], end_dates=[])
    base.update(kw)
    return base


def test_strong_valid_page_for_gap_business_is_added() -> None:
    verdict = review_page(_page(sources=["map", "search"]), _biz(never_processed=True), TODAY)
    assert verdict["decision"] == "add" and verdict["score"] >= 65
    assert "business_never_processed" in verdict["reasons"]


def test_needs_review_never_becomes_a_plain_add() -> None:
    verdict = review_page(_page(status="needs_review", sources=["map", "search"]), _biz(never_processed=True), TODAY)
    assert verdict["decision"] == "add_with_review" and "visibility_needs_review" in verdict["reasons"]


def test_skip_reasons() -> None:
    assert review_page(_page(status="rejected"), _biz(), TODAY)["decision"] == "skip"
    assert review_page(_page(url="https://www.groupon.com/biz/x/spa"), _biz(), TODAY)["reasons"] == ["aggregator_or_directory"]
    assert review_page(_page(url="https://spa.com/"), _biz(), TODAY)["reasons"] == ["homepage"]
    assert review_page(_page(evidence=[]), _biz(), TODAY)["reasons"] == ["no_evidence"]
    expired = review_page(_page(end_dates=["2026-09-30"]), _biz(), TODAY)
    assert expired["decision"] == "skip" and expired["reasons"][0].startswith("expired_")
    upcoming = review_page(_page(end_dates=["2026-11-30"]), _biz(), TODAY)
    assert upcoming["decision"] != "skip" and any(r.startswith("ends_") for r in upcoming["reasons"])


def test_blog_and_below_line_business_are_penalised() -> None:
    strong = review_page(_page(), _biz(), TODAY)["score"]
    blog = review_page(_page(url="https://spa.com/blog/fall-specials"), _biz(), TODAY)["score"]
    below = review_page(_page(), _biz(score=3.5), TODAY)["score"]
    assert blog < strong and below < strong


def test_decide_dedupes_and_caps_per_business() -> None:
    pages = [_page(url=f"https://spa.com/offers/{i}", evidence=[f"Botox ${10 + i} per unit"]) for i in range(9)]
    pages.append(_page(url="https://spa.com/offers/0", evidence=["Botox $10 per unit", "Filler $499 per syringe"]))
    records = decide(pages, {1: _biz(never_processed=True)}, TODAY)
    assert len(records) == 9  # duplicate url collapsed
    kept = [r for r in records if r["decision"] != "skip"]
    assert len(kept) == 6 and any("business_page_cap" in r["reasons"] for r in records)


def test_branches_sharing_one_page_each_keep_a_decision() -> None:
    page = _page(url="https://laseraway.com/promotions", evidence=["Your First Botox Treatment Is $10 Per Unit"])
    businesses = {1: _biz(never_processed=True), 2: _biz(business_id=2, never_processed=True)}
    records = decide([{**page, "business_id": 1}, {**page, "business_id": 2}], businesses, TODAY)
    assert sorted((r["url"], r["business_id"]) for r in records) == [("https://laseraway.com/promotions", 1),
                                                                    ("https://laseraway.com/promotions", 2)]


def test_boilerplate_evidence_repeated_across_pages_counts_once() -> None:
    shared = ["Get $50 off your first visit. Book now."]
    pages = [_page(url="https://spa.com/botox-cost", evidence=shared),
             _page(url="https://spa.com/filler-cost", evidence=shared),
             _page(url="https://spa.com/prp-cost", evidence=shared + ["PRP facial $450 per session"])]
    records = {r["url"]: r for r in decide(pages, {1: _biz()}, TODAY)}
    kept = [u for u, r in records.items() if r["decision"] != "skip"]
    # the PRP page carries the shared line plus a real price, so the two boilerplate-only pages add nothing
    assert kept == ["https://spa.com/prp-cost"]
    dropped = [r for r in records.values() if r["decision"] == "skip"]
    assert len(dropped) == 2 and all(r["reasons"][-1] == "duplicate_evidence" for r in dropped)


def test_cost_explainer_articles_with_market_ranges_are_skipped() -> None:
    article = _page(url="https://spa.com/how-much-does-botox-cost", kind="pricing",
                    evidence=["The average price of Botox per unit ranges from $11 to $15.",
                              "Liquid rhinoplasty typically ranges from $600 to $1,500 per session."])
    assert review_page(article, _biz(), TODAY)["reasons"] == ["informational_article"]
    own_price = _page(url="https://spa.com/how-much-does-botox-cost", kind="pricing",
                      evidence=["Botox $12 per unit at our clinic", "Dysport $4 per unit"])
    assert review_page(own_price, _biz(), TODAY)["decision"] != "skip"
    membership = _page(url="https://spa.com/membership-faq", kind="membership",
                       evidence=["Memberships typically start at $99 per month"])
    assert review_page(membership, _biz(), TODAY)["decision"] != "skip"
