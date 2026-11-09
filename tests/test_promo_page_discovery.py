from __future__ import annotations

from utils.promo_page_discovery import assess_offer_page, candidate_urls, search_candidates

FILLER = "Our medspa offers a full range of aesthetic services for every patient in town. " * 6


def _html(body: str) -> str:
    return f"<html><body><main>{FILLER}{body}</main></body></html>"


def test_candidates_are_same_site_new_and_prioritised() -> None:
    mapped = [
        "https://www.spa.com/", "https://spa.com/specials", "https://spa.com/membership/", "https://spa.com/pricing",
        "https://spa.com/blog/fall-specials", "https://spa.com/about", "https://other.com/specials",
        "https://book.spa.com/offers", "https://spa.com/wp-content/x/offers.pdf", "https://spa.com/specials/",
        "https://spa.com/tag/promo",
    ]
    out = candidate_urls("https://www.spa.com", mapped, known={"https://spa.com/pricing"})
    urls = [c["url"] for c in out]
    assert urls == ["https://spa.com/specials", "https://book.spa.com/offers", "https://spa.com/membership"]
    assert out[0]["kind_hint"] == "promotion" and out[2]["kind_hint"] == "membership"
    assert [c["url"] for c in candidate_urls("https://spa.com", mapped, allow_blog=True, limit=20) if "blog" in c["url"]]


def test_percent_and_amount_off_with_service_is_valid_promotion() -> None:
    md = "# Fall specials\nBotox $11 per unit this month\n20% off all laser hair removal packages"
    result = assess_offer_page(md, _html("<p>Botox $11 per unit this month</p><p>20% off all laser hair removal packages</p>"))
    assert result.valid and result.kind == "promotion" and not result.needs_review
    assert any("20% off" in e for e in result.evidence)


def test_banner_repeated_with_footnote_spacing_counts_as_one_evidence_line() -> None:
    md = ("75% OFF LASER HAIR REMOVAL ǂ\n\n75% OFF LASER HAIR REMOVALǂ\n\n"
          "### 50% Savings On CoolSculpting (8+ Treatments)\\*\\*\n\n### Your First Botox Treatment Is $10 Per Unit‡‡")
    result = assess_offer_page(md, None)
    assert list(result.evidence) == ["75% OFF LASER HAIR REMOVAL ǂ",
                                     "### 50% Savings On CoolSculpting (8+ Treatments)\\*\\*",
                                     "### Your First Botox Treatment Is $10 Per Unit‡‡"]


def test_monthly_price_is_membership() -> None:
    md = "VIP Membership $99/month includes one facial"
    result = assess_offer_page(md, _html("<p>VIP Membership $99/month includes one facial</p>"), kind_hint="membership")
    assert result.valid and result.kind == "membership"


def test_page_without_price_or_discount_is_rejected() -> None:
    md = "Welcome to our specials page. Ask us about current offers and book today."
    result = assess_offer_page(md, _html("<p>Ask us about current offers</p>"))
    assert not result.valid and "no_discount_or_price_evidence" in result.reasons


def test_hidden_price_is_not_valid() -> None:
    md = "Botox $9 per unit"
    html = f'<html><body>{FILLER}<p style="display:none">Botox $9 per unit</p></body></html>'
    result = assess_offer_page(md, html)
    assert not result.valid and "evidence_not_in_visible_text" in result.reasons


def test_missing_html_needs_review_not_valid_silently() -> None:
    result = assess_offer_page("Botox $10 per unit", None)
    assert result.valid and result.needs_review and "no_html_for_visibility" in result.reasons


def test_css_class_hiding_marks_review() -> None:
    html = _html('<p>Botox $10 per unit</p><div class="d-none">other</div>')
    result = assess_offer_page("Botox $10 per unit", html)
    assert result.valid and result.needs_review


def test_implausible_amounts_are_ignored() -> None:
    result = assess_offer_page("Call 555 for $1 off your first visit", _html("<p>Call 555 for $1 off</p>"))
    assert not result.valid


def test_path_words_not_substrings() -> None:
    mapped = ["https://spa.com/ideal-frequency-for-dysport", "https://spa.com/waterfall-facial",
              "https://spa.com/prevent-aging", "https://spa.com/black-friday-deals", "https://spa.com/beauty-bank",
              "https://spa.com/new-client-offer", "https://spa.com/our-team"]
    got = {c["url"].rsplit("/", 1)[1]: c["kind_hint"] for c in candidate_urls("https://spa.com", mapped, limit=20)}
    assert got == {"black-friday-deals": "promotion", "beauty-bank": "membership", "new-client-offer": "promotion"}


def test_compare_at_sale_price_is_a_discount() -> None:
    md = "Regular price ~$600.00 USD~ Sale price $420.00 USD"
    result = assess_offer_page(md, _html("<p>Regular price <s>$600.00 USD</s> Sale price $420.00 USD</p>"))
    assert result.valid and result.kind == "promotion"
    plain = assess_offer_page("Regular price $420.00 USD", _html("<p>Regular price $420.00 USD</p>"))
    assert not plain.valid  # a bare list price is not a discount


def test_url_encoded_percent_is_not_a_discount() -> None:
    md = "[HydraFacial](https://spa.com/collections/hydrafacial%E2%84%A2)"
    assert not assess_offer_page(md, _html("<p>x</p>")).valid


def test_language_duplicates_and_zero_price_are_dropped() -> None:
    mapped = ["https://spa.com/pages/deals", "https://spa.com/es/pages/deals", "https://spa.com/es/pages/only-spanish-offers"]
    urls = [c["url"] for c in candidate_urls("https://spa.com", mapped, limit=10)]
    assert urls == ["https://spa.com/pages/deals", "https://spa.com/es/pages/only-spanish-offers"]
    free = assess_offer_page("Regular price Sale price $0.00 USD", _html("<p>Regular price Sale price $0.00 USD</p>"))
    assert not free.valid


def test_free_consultation_is_not_a_price_discount() -> None:
    md = "Free Skincare Consultation with our wellness team. Book today!"
    assert not assess_offer_page(md, _html("<p>Free Skincare Consultation with our wellness team</p>")).valid
    bogo = assess_offer_page("Buy 2 syringes of filler, get 1 free", _html("<p>Buy 2 syringes of filler, get 1 free</p>"))
    assert bogo.valid


HUB_HTML = (
    "<html><body><header><a href='/pages/botox-specials'>nav promo</a><a href='/services/laser'>Laser</a></header>"
    "<main><a href='/products/hydrafacial-duo-promo'>Duo</a><a href='/collections/packages'>Packages</a>"
    "<a href='/pages/black-friday-deals'>self</a><a href='/es/products/hydrafacial-duo-promo'>es</a>"
    "<a href='https://instagram.com/spa'>ig</a><a href='/blog/news'>blog</a><a href='/about'>about</a>"
    "<a href='/products/known-thing'>k</a><a href='mailto:a@b.c'>m</a></main>"
    "<footer><a href='/pages/footer-specials'>footer</a></footer></body></html>"
)


def test_hub_links_use_main_content_only() -> None:
    from utils.promo_page_discovery import hub_links

    links = hub_links(HUB_HTML, "https://spa.com/pages/black-friday-deals",
                      known={"https://spa.com/products/known-thing"})
    assert set(links) == {"https://spa.com/products/hydrafacial-duo-promo", "https://spa.com/collections/packages"}


def test_hub_detection_needs_a_promo_page_without_price() -> None:
    from utils.promo_page_discovery import is_hub_page

    empty = assess_offer_page("Our specials: see below", _html("<p>Our specials: see below</p>"))
    assert is_hub_page(empty, "promotion") and is_hub_page(empty, "membership")
    assert not is_hub_page(empty, "pricing")
    priced = assess_offer_page("Botox $11 per unit", _html("<p>Botox $11 per unit</p>"))
    assert not is_hub_page(priced, "promotion")


def test_specialty_and_navigation_pages_are_not_offers_or_hops() -> None:
    from utils.promo_page_discovery import hub_links

    assert candidate_urls("https://spa.com", ["https://spa.com/treatments/specialty-skin-rejuvenation",
                                              "https://spa.com/specialist-team", "https://spa.com/specials"],
                          limit=10) == [{"url": "https://spa.com/specials", "kind_hint": "promotion"}]
    html = ("<main><a href='/collections/facials'>Facials</a><a href='/treatments/laser'>Laser</a>"
            "<a href='/products/botox-promo-49'>Botox</a></main>")
    assert hub_links(html, "https://spa.com/pages/deals") == ["https://spa.com/products/botox-promo-49"]
    assert not candidate_urls("https://spa.com", ["https://spa.com/event-rsvp", "https://spa.com/rewards-club-terms-and-conditions"])
    # 条款页（laseraway.com/offer-terms）是卡片的补充证据，不是促销来源
    assert not candidate_urls("https://spa.com", ["https://spa.com/offer-terms", "https://spa.com/specials-terms/fall"])
    assert not search_candidates("https://spa.com", [{"url": "https://spa.com/offer-terms", "title": "Offer Terms | 20% off"}])


def test_search_candidates_filter_site_aggregators_and_titles() -> None:
    from utils.promo_page_discovery import search_candidates

    results = [
        {"url": "https://spa.com/products/botox-50-off?srsltid=abc", "title": "Botox 50% OFF first treatment"},
        {"url": "https://www.groupon.com/biz/irvine/spa", "title": "Spa promotions"},
        {"url": "https://spa.com/blog/fall", "title": "Fall specials"},
        {"url": "https://spa.com/about-us", "title": "About our team"},
        {"url": "https://spa.com/", "title": "Home | specials"},
        {"url": "https://spa.com/specials", "title": "Specials"},
        {"url": "https://spa.com/known", "title": "Deal"},
    ]
    got = search_candidates("https://spa.com", results, known={"https://spa.com/known"})
    assert [c["url"] for c in got] == ["https://spa.com/products/botox-50-off", "https://spa.com/specials"]
    assert got[0]["kind_hint"] == "promotion"


def test_end_dates_with_and_without_year() -> None:
    import datetime as dt

    from utils.promo_page_discovery import extract_end_dates

    today = dt.date(2026, 10, 15)
    text = "Offer ends Nov 30, 2026. Valid through 12/31. Expires Jan 5. Not related: January 3 open house."
    # 月日没有年份时取当前年份：一月初结束的活动在十月已经过期
    assert extract_end_dates(text, today) == ["2026-01-05", "2026-11-30", "2026-12-31"]
    assert extract_end_dates("Valid through September 1", today) == ["2026-09-01"]
    assert extract_end_dates("Offer ends Mar 31", dt.date(2026, 11, 9)) == ["2026-03-31"]
    assert extract_end_dates("until 3/1/25", today) == ["2025-03-01"]
    assert extract_end_dates("no dates here", today) == []
