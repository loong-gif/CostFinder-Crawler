from __future__ import annotations

import pytest

from utils.promo_page_discovery import assess_offer_page, candidate_urls

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


class _FakeFirecrawl:
    def __init__(self, pages):
        self.pages, self.scraped = pages, []

    def map(self, url, limit):
        return ["https://spa.com/", "https://spa.com/pages/deals"]

    def links(self, url):
        return []

    def scrape(self, url):
        self.scraped.append(url)
        return self.pages[url]


def test_discover_business_follows_a_priceless_hub_one_hop() -> None:
    import argparse

    from scripts.discover_new_promo_pages import discover_business

    hub_html = ("<html><body><main>Our current deals. See each offer below.<a href='/products/duo-promo'>Duo</a>"
                "<a href='/products/free-consult'>Consult</a></main></body></html>")
    fc = _FakeFirecrawl({
        "https://spa.com/pages/deals": {"markdown": "Our current deals. See each offer below.", "rawHtml": hub_html},
        "https://spa.com/products/duo-promo": {
            "markdown": "Regular price ~$600.00 USD~ Sale price $420.00 USD",
            "rawHtml": _html("<p>Regular price <s>$600.00 USD</s> Sale price $420.00 USD</p>")},
        "https://spa.com/products/free-consult": {
            "markdown": "Free consultation", "rawHtml": _html("<p>Free consultation</p>")},
    })
    args = argparse.Namespace(map_limit=100, allow_blog=False, max_candidates=5, hops=1, max_hop_links=8, max_pages=10,
                               sources="map", search_limit=10)
    result = discover_business(fc, {"business_id": 1, "name": "Spa", "city": "X", "website": "https://spa.com"},
                               set(), args)
    by_url = {p["url"]: p for p in result["pages"]}
    assert by_url["https://spa.com/pages/deals"]["status"] == "rejected" and by_url["https://spa.com/pages/deals"]["hop"] == 0
    duo = by_url["https://spa.com/products/duo-promo"]
    assert duo["hop"] == 1 and duo["via"] == "https://spa.com/pages/deals" and duo["status"] in ("valid", "needs_review")
    assert by_url["https://spa.com/products/free-consult"]["status"] == "rejected"
    # hops disabled: the hub is not followed
    fc2 = _FakeFirecrawl(fc.pages)
    args.hops = 0
    assert len(discover_business(fc2, {"business_id": 1, "name": "Spa", "city": "X", "website": "https://spa.com"},
                                 set(), args)["pages"]) == 1
    # page budget caps the work
    args.hops, args.max_pages = 1, 2
    fc3 = _FakeFirecrawl(fc.pages)
    assert len(discover_business(fc3, {"business_id": 1, "name": "Spa", "city": "X", "website": "https://spa.com"},
                                 set(), args)["pages"]) == 2


def test_specialty_and_navigation_pages_are_not_offers_or_hops() -> None:
    from utils.promo_page_discovery import hub_links

    assert candidate_urls("https://spa.com", ["https://spa.com/treatments/specialty-skin-rejuvenation",
                                              "https://spa.com/specialist-team", "https://spa.com/specials"],
                          limit=10) == [{"url": "https://spa.com/specials", "kind_hint": "promotion"}]
    html = ("<main><a href='/collections/facials'>Facials</a><a href='/treatments/laser'>Laser</a>"
            "<a href='/products/botox-promo-49'>Botox</a></main>")
    assert hub_links(html, "https://spa.com/pages/deals") == ["https://spa.com/products/botox-promo-49"]
    assert not candidate_urls("https://spa.com", ["https://spa.com/event-rsvp", "https://spa.com/rewards-club-terms-and-conditions"])


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
    assert extract_end_dates(text, today) == ["2026-11-30", "2026-12-31", "2027-01-05"]
    assert extract_end_dates("Valid through September 1", today) == ["2026-09-01"]  # recently ended, same year
    assert extract_end_dates("until 3/1/25", today) == ["2025-03-01"]
    assert extract_end_dates("no dates here", today) == []


def test_search_and_crawl_sources_add_candidates_without_a_second_scrape() -> None:
    import argparse

    from scripts.discover_new_promo_pages import discover_business

    class Fc(_FakeFirecrawl):
        def map(self, url, limit):
            return ["https://spa.com/"]  # JS site: only the home page

        def search(self, query, limit=10):
            return [{"url": "https://spa.com/products/botox-promo?srsltid=x", "title": "Botox $9 per unit promo"}]

    pages = {"https://spa.com/products/botox-promo": {
        "markdown": "Botox $9 per unit", "rawHtml": _html("<p>Botox $9 per unit</p>")}}
    fc = Fc(pages)
    args = argparse.Namespace(map_limit=100, allow_blog=False, max_candidates=5, hops=0, max_hop_links=8, max_pages=10,
                              sources="map,search", search_limit=10)
    result = discover_business(fc, {"business_id": 2, "name": "Spa", "city": "X", "website": "https://spa.com"}, set(), args)
    found = {p["url"]: p for p in result["pages"]}
    assert found["https://spa.com/products/botox-promo"]["sources"] == ["search"]
    assert found["https://spa.com/products/botox-promo"]["title"].startswith("Botox")
    assert fc.scraped == ["https://spa.com/products/botox-promo"]


def test_map_reads_v2_map_link_objects(monkeypatch) -> None:
    from scripts.discover_new_promo_pages import Firecrawl

    posted = []

    def post(self, path, body):
        posted.append((path, body))
        return {"success": True, "links": [{"url": "https://spa.com/specials", "title": "Specials"},
                                           {"url": "https://spa.com/memberships"}]}

    monkeypatch.setattr(Firecrawl, "_post", post)
    links = Firecrawl("http://127.0.0.1:3002").map("https://spa.com", 50)
    assert links == ["https://spa.com/specials", "https://spa.com/memberships"]
    assert posted == [("/v2/map", {"url": "https://spa.com", "limit": 50})]


def test_empty_search_is_retried_and_a_dead_backend_is_not_hammered() -> None:
    import argparse
    import threading

    from scripts.discover_new_promo_pages import SearchHealth, discover_business

    class Dead(_FakeFirecrawl):
        searches = 0

        def map(self, url, limit):
            return []

        def search(self, query, limit=10):
            Dead.searches += 1
            return []

    health = SearchHealth(empty_limit=2, cooldown=0)
    args = argparse.Namespace(map_limit=10, allow_blog=False, max_candidates=5, hops=0, max_hop_links=8, max_pages=5,
                              sources="search", search_limit=10, search_retry_delay=0, search_health=health)
    fc = Dead({})
    statuses = [discover_business(fc, {"business_id": i, "name": "S", "city": "X", "website": f"https://s{i}.com"},
                                  set(), args)["search_status"] for i in range(5)]
    assert health.down and statuses[0] == "no_results"
    assert statuses[-1] == "skipped_engines_down"
    searches_before = Dead.searches
    discover_business(fc, {"business_id": 9, "name": "S", "city": "X", "website": "https://s9.com"}, set(), args)
    assert Dead.searches == searches_before  # a down backend is skipped, not queried

    class Healthy(_FakeFirecrawl):
        def map(self, url, limit):
            return []

        def search(self, query, limit=10):
            return [{"url": "https://s1.com/specials", "title": "Specials"}]

    args.search_health = SearchHealth()
    ok = discover_business(Healthy({"https://s1.com/specials": {"markdown": "Botox $9 per unit",
                           "rawHtml": _html("<p>Botox $9 per unit</p>")}}),
                           {"business_id": 1, "name": "S", "city": "X", "website": "https://s1.com"}, set(), args)
    assert ok["search_status"] == "ok" and ok["search_results"] == 1


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _CloudSession:
    def __init__(self, responses):
        self.responses, self.calls, self.trust_env = list(responses), [], True

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(json)
        return self.responses.pop(0)

    def get(self, url, headers=None, timeout=None):
        return _Resp(200, {"data": {"remainingCredits": 500}})


def test_cloud_search_parses_v2_and_enforces_the_budget() -> None:
    from scripts.discover_new_promo_pages import BudgetExhausted, CloudSearch

    ok = _Resp(200, {"success": True, "creditsUsed": 2,
                     "data": {"web": [{"url": "https://spa.com/specials", "title": "Specials"}]}})
    session = _CloudSession([ok, ok, ok])
    search = CloudSearch("fc-x", budget=4, reserve=100, interval=0, session=session)
    assert search.effective_budget() == 4  # min(budget 4, remaining 500 - reserve 100)
    assert search.search("q", 25)[0]["url"] == "https://spa.com/specials"
    assert session.calls[0]["limit"] == 10  # limit is capped at 10: more results never cost less
    search.search("q")
    assert search.spent == 4 and search.searches == 2
    with pytest.raises(BudgetExhausted):
        search.search("q")
    assert len(session.calls) == 2  # the over-budget search never reached the API


def test_cloud_budget_never_spends_into_the_reserve() -> None:
    from scripts.discover_new_promo_pages import CloudSearch

    search = CloudSearch("fc-x", budget=10_000, reserve=450, interval=0, session=_CloudSession([]))
    assert search.effective_budget() == 50  # 500 remaining - 450 reserve


def test_out_of_credits_stops_spending_and_marks_businesses_skipped() -> None:
    import argparse

    from scripts.discover_new_promo_pages import CloudSearch, SearchHealth, discover_business

    class Fc(_FakeFirecrawl):
        def map(self, url, limit):
            return []

    session = _CloudSession([_Resp(402, {"error": "Insufficient credits"})])
    args = argparse.Namespace(map_limit=10, allow_blog=False, max_candidates=5, hops=0, max_hop_links=8, max_pages=5,
                              sources="search", search_limit=10, search_retry_delay=0, search_health=SearchHealth(),
                              search_client=CloudSearch("fc-x", budget=100, interval=0, session=session))
    result = discover_business(Fc({}), {"business_id": 1, "name": "S", "city": "X", "website": "https://s.com"},
                               set(), args)
    assert result["search_status"] == "skipped_budget"
    assert args.search_client.budget == args.search_client.spent == 0


def test_branches_sharing_a_website_are_discovered_once() -> None:
    from scripts.discover_new_promo_pages import discover_per_site

    rows = [{"business_id": 4385, "name": "LaserAway Morningside Heights", "city": "New York",
             "website": "https://www.laseraway.com/locations/ny/manhattan-morningside-heights/?utm_source=google"},
            {"business_id": 7, "name": "Glow Spa", "city": "Austin", "website": "https://glowspa.com"},
            {"business_id": 4386, "name": "LaserAway Huntington Beach", "city": "Huntington Beach",
             "website": "https://www.laseraway.com/locations/ca/huntington-beach/"}]
    discovered: list[int] = []

    def discover(business):
        discovered.append(business["business_id"])
        return {"business_id": business["business_id"], "name": business["name"], "city": business["city"],
                "website": business["website"], "mapped": 1, "candidates": 1, "error": None,
                "pages": [{"url": f"https://{business['website'].split('/')[2]}/promotions", "status": "needs_review"}]}

    results = discover_per_site(rows, discover)

    assert discovered == [4385, 7]
    assert [r["business_id"] for r in results] == [4385, 7, 4386]
    assert results[2]["pages"] == results[0]["pages"] == [{"url": "https://www.laseraway.com/promotions",
                                                           "status": "needs_review"}]
    assert results[2]["city"] == "Huntington Beach" and results[2]["shared_site_with"] == 4385
    assert "shared_site_with" not in results[0]
