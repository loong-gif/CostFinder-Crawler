from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

FILLER = "Our medspa offers a full range of aesthetic services for every patient in town. " * 6


def _html(body: str) -> str:
    return f"<html><body><main>{FILLER}{body}</main></body></html>"


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

    from utils.page_discovery_run import discover_business

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


def test_search_and_crawl_sources_add_candidates_without_a_second_scrape() -> None:
    import argparse

    from utils.page_discovery_run import discover_business

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
    from utils.page_discovery_run import Firecrawl

    posted = []

    def post(self, path, body):
        posted.append((path, body))
        return {"success": True, "links": [{"url": "https://spa.com/specials", "title": "Specials"},
                                           {"url": "https://spa.com/memberships"}]}

    monkeypatch.setattr(Firecrawl, "_post", post)
    links = Firecrawl("http://127.0.0.1:3002").map("https://spa.com", 50)
    assert links == ["https://spa.com/specials", "https://spa.com/memberships"]
    assert posted == [("/v2/map", {"url": "https://spa.com", "limit": 50})]


def test_topic_search_seeds_are_assessed_first_for_their_site() -> None:
    import argparse

    from utils.page_discovery_run import discover_business

    class Fc(_FakeFirecrawl):
        def map(self, url, limit):
            return ["https://spa.com/", "https://spa.com/specials"]

    pages = {"https://spa.com/botox-special": {"markdown": "Botox $10 per unit special",
                                               "rawHtml": _html("<p>Botox $10 per unit special</p>")},
             "https://spa.com/specials": {"markdown": "Filler $500 special", "rawHtml": _html("<p>Filler $500 special</p>")}}
    fc = Fc(pages)
    seeds = {"spa.com": [{"url": "https://spa.com/botox-special", "kind_hint": "promotion", "title": "Botox Special",
                          "query": "botox specials Los Angeles"},
                         {"url": "https://spa.com/specials", "kind_hint": "promotion", "title": "Specials", "query": "q"}]}
    args = argparse.Namespace(map_limit=100, allow_blog=False, max_candidates=5, hops=0, max_hop_links=8, max_pages=10,
                              sources="map", search_limit=10, seeds=seeds)
    result = discover_business(fc, {"business_id": 2, "name": "Spa", "city": "X", "website": "https://www.spa.com"},
                               set(), args)
    found = {p["url"]: p for p in result["pages"]}
    assert fc.scraped[0] == "https://spa.com/botox-special"
    assert found["https://spa.com/botox-special"]["sources"] == ["topic_search"]
    # map 也找到的种子页面两个来源都记下
    assert found["https://spa.com/specials"]["sources"] == ["topic_search", "map"]


def test_empty_search_is_retried_and_a_dead_backend_is_not_hammered() -> None:
    import argparse

    from utils.discovery_search import SearchHealth
    from utils.page_discovery_run import discover_business

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
    from utils.discovery_search import BudgetExhausted, CloudSearch

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
    from utils.discovery_search import CloudSearch

    search = CloudSearch("fc-x", budget=10_000, reserve=450, interval=0, session=_CloudSession([]))
    assert search.effective_budget() == 50  # 500 remaining - 450 reserve


def test_out_of_credits_stops_spending_and_marks_businesses_skipped() -> None:
    import argparse

    from utils.discovery_search import CloudSearch, SearchHealth
    from utils.page_discovery_run import discover_business

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
    from utils.page_discovery_run import discover_per_site

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


def test_naked_domain_businesses_use_their_real_site_roots_for_grouping() -> None:
    from utils.page_discovery_run import discover_business, discover_per_site

    # 裸域名摘自 output/monitor_price_sync/expired_recheck_20261021/database_snapshot.json。
    rows = json.loads((Path(__file__).parent / "fixtures/discovery/naked_domain_businesses.json").read_text())
    args = Namespace(sources="", seeds={}, allow_blog=False, max_candidates=8, hops=0, max_hop_links=0, max_pages=0)
    discovered: list[int] = []

    def discover(business):
        discovered.append(business["business_id"])
        return discover_business(None, business, set(), args)

    results = discover_per_site(rows, discover)

    assert discovered == [47, 51]
    assert [row["business_id"] for row in results] == [47, 51]
    assert all("shared_site_with" not in row for row in results)


def test_businesses_that_failed_or_hit_a_down_search_engine_are_rerun_until_stable() -> None:
    from utils.page_discovery_run import discover_until_stable

    rows = [{"business_id": i} for i in (1, 2, 3, 4)]
    # 第 1 轮：2 出错、3 搜索引擎不可用、4 云端额度用尽；第 2 轮 2 恢复，3 仍不可用；第 3 轮 3 仍不可用，停止
    outcomes = {1: [{}], 2: [{"error": "RemoteDisconnected"}, {}], 3: [{"search_status": "skipped_engines_down"}] * 3,
                4: [{"search_status": "skipped_budget"}]}
    rounds, waits, resets = [], [], []

    def run_round(group):
        rounds.append([row["business_id"] for row in group])
        return [{"business_id": row["business_id"], "error": None, "search_status": "ok",
                 **outcomes[row["business_id"]].pop(0)} for row in group]

    results = discover_until_stable(rows, run_round, rounds=4, pause=60, sleep=waits.append,
                                    reset=lambda: resets.append(1))
    # 额度用尽在同一次运行内重试无益，只有出错与搜索引擎不可用的商家重跑；一轮没有任何商家恢复即为稳定
    assert rounds == [[1, 2, 3, 4], [2, 3], [3]]
    assert waits == [60, 120] and len(resets) == 2
    assert [r["business_id"] for r in results] == [1, 2, 3, 4]
    assert results[1]["error"] is None and results[2]["search_status"] == "skipped_engines_down"


def test_rerunning_a_batch_skips_finished_businesses_and_keeps_their_results() -> None:
    from utils.page_discovery_run import finished, merge_results

    earlier = [{"business_id": 1, "error": None, "search_status": "ok", "pages": [{"url": "a"}]},
               {"business_id": 2, "error": "Timeout", "pages": []},
               {"business_id": 3, "error": None, "search_status": "skipped_budget", "pages": []},
               {"business_id": 4, "error": None, "search_status": "no_results", "pages": []}]
    assert [r["business_id"] for r in earlier if finished(r)] == [1, 4]
    fresh = [{"business_id": 2, "error": None, "search_status": "ok", "pages": [{"url": "b"}]},
             {"business_id": 3, "error": None, "search_status": "ok", "pages": []},
             {"business_id": 9, "error": None, "search_status": "ok", "pages": []}]
    merged = merge_results(earlier, fresh)
    assert [(r["business_id"], r["search_status"]) for r in merged] == [(1, "ok"), (2, "ok"), (3, "ok"), (4, "no_results"),
                                                                        (9, "ok")]


def test_empty_topic_search_seeds_select_no_real_business_sites() -> None:
    from utils.page_discovery_run import select_discovery_businesses

    fixture_dir = Path(__file__).parent / "fixtures"
    seed_path = fixture_dir / "topic_search/la_botox_search_seeds.json"
    # 空 seeds 复制自 output/pipeline/topic_search/la_botox/search/seeds.json。
    seeds = json.loads(seed_path.read_text())
    rows = json.loads((fixture_dir / "discovery/naked_domain_businesses.json").read_text())

    assert seeds == {}
    assert select_discovery_businesses(rows, set(), seeds) == []
    assert select_discovery_businesses(rows, {51}, seeds) == [rows[1]]
    assert select_discovery_businesses(rows, set(), None) == rows
