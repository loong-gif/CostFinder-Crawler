from utils.topic_search import expand_queries, match_hits, run_topic_search, search_report

PLACES = [
    {"google_place_id": "a1", "name": "OU Beauty", "city": "Los Angeles", "website": "https://oubeauty.com/"},
    {"google_place_id": "k1", "name": "Kalologie Studio City", "city": "Studio City", "website": "https://kalologie.com/"},
    {"google_place_id": "k2", "name": "Kalologie Brentwood", "city": "Los Angeles", "website": "https://www.kalologie.com"},
    {"google_place_id": "s1", "name": "Swan Center", "city": "Atlanta", "website": "https://swancenteratlanta.com"},
    {"google_place_id": "n1", "name": "No Site Spa", "city": "Los Angeles", "website": ""},
    {"google_place_id": "p1", "name": "Carrie's Pilates", "city": "Los Angeles", "website": "https://carriespilatesplus.com",
     "category": "Physical fitness program"},
]
HITS = [
    {"query": "botox specials Los Angeles", "url": "https://oubeauty.com/botox-special", "title": "Botox Special $10/unit",
     "description": ""},
    {"query": "botox specials Los Angeles", "url": "https://oubeauty.com/about", "title": "About us", "description": ""},
    {"query": "botox specials Studio City", "url": "https://kalologie.com/", "title": "Kalologie", "description": ""},
    {"query": "botox specials Los Angeles", "url": "https://www.groupon.com/local/los-angeles/botox", "title": "Deals",
     "description": ""},
    {"query": "botox specials Los Angeles", "url": "https://www.vixenaesthetics.com/", "title": "Shelby Township, MI",
     "description": ""},
    {"query": "botox specials Los Angeles", "url": "https://swancenteratlanta.com/specials", "title": "Specials",
     "description": ""},
    {"query": "botox specials Los Angeles", "url": "https://westhollywood.carriespilatesplus.com/pricing",
     "title": "Excellent Price For Sessions", "description": ""},
    {"query": "botox specials Los Angeles", "url": "https://www.zocdoc.com/cosmetic-botox", "title": "Botox doctors",
     "description": ""},
]
CITIES = ["Los Angeles", "Studio City", "Beverly Hills"]


def test_queries_cover_every_area_with_every_template():
    assert expand_queries(["botox specials {area}", "botox deal {area}"], ["Los Angeles", "Encino"]) == [
        "botox specials Los Angeles", "botox deal Los Angeles", "botox specials Encino", "botox deal Encino"]


def test_hits_are_kept_only_for_clinics_in_the_target_cities():
    matched = match_hits(HITS, PLACES, CITIES)
    clinics = {c["root"]: c for c in matched["clinics"]}
    assert set(clinics) == {"oubeauty.com", "kalologie.com"}
    # 同一网站的每家目标城市分店都注册
    assert {p["google_place_id"] for p in clinics["kalologie.com"]["places"]} == {"k1", "k2"}
    # 只有像促销页的同站命中成为候选页面，并记下来自哪条查询
    assert clinics["oubeauty.com"]["candidates"] == [{"url": "https://oubeauty.com/botox-special", "kind_hint": "promotion",
                                                      "title": "Botox Special $10/unit",
                                                      "query": "botox specials Los Angeles"}]
    assert clinics["kalologie.com"]["candidates"] == []
    assert [c["root"] for c in matched["out_of_area"]] == ["swancenteratlanta.com"]
    # 聚合站与目录站不算网站；没有对应诊所（或只有非诊所类别的地点）的命中归入 unmatched
    assert [c["root"] for c in matched["unmatched"]] == ["carriespilatesplus.com", "vixenaesthetics.com"]


def test_report_measures_how_much_of_the_search_lands_on_target_clinics():
    report = search_report(HITS, match_hits(HITS, PLACES, CITIES))
    assert report["hits"] == 8 and report["clinic_hits"] == 3 and report["clinic_hit_rate"] == 0.375
    assert report["clinic_sites"] == 2 and report["clinic_places"] == 3
    assert report["promotion_page_candidates"] == 1
    assert report["out_of_area_sites"] == ["swancenteratlanta.com"] and report["unmatched_sites"] == 2


def test_search_runs_each_query_and_keeps_canonical_urls():
    asked = []

    def search(query, limit):
        asked.append((query, limit))
        return [{"url": "https://oubeauty.com/botox-special?utm_source=x", "title": "Special"}, {"url": ""}]

    hits = run_topic_search(search, ["q1", "q2"], limit=7, interval=0)
    assert asked == [("q1", 7), ("q2", 7)]
    assert [h["url"] for h in hits] == ["https://oubeauty.com/botox-special"] * 2


def test_unmatched_sites_are_looked_up_in_google_maps_until_no_new_site_appears():
    from utils.topic_search import lookup_keyword, match_with_lookups

    assert lookup_keyword("vixenaesthetics.com", "Los Angeles", "CA") == "vixenaesthetics Los Angeles, CA"
    found = {"carriespilatesplus.com": [], "vixenaesthetics.com": [
        {"google_place_id": "v1", "name": "Vixen Aesthetics", "city": "Beverly Hills",
         "website": "https://vixenaesthetics.com/"}]}
    asked = []

    def lookup(root):
        asked.append(root)
        return found[root]

    saved = []
    matched, places, lookups = match_with_lookups(HITS, PLACES, CITIES, lookup, done={}, save=saved.append)
    assert asked == ["carriespilatesplus.com", "vixenaesthetics.com"]
    assert [c["root"] for c in matched["clinics"]] == ["kalologie.com", "oubeauty.com", "vixenaesthetics.com"]
    assert [c["root"] for c in matched["unmatched"]] == ["carriespilatesplus.com"]
    assert "v1" in {p["google_place_id"] for p in places}
    # 每查完一个站点就保存，命令中断后重新运行不重复查询
    assert [sorted(s) for s in saved] == [["carriespilatesplus.com"], ["carriespilatesplus.com", "vixenaesthetics.com"]]
    asked.clear()
    again, _, _ = match_with_lookups(HITS, PLACES, CITIES, lookup, done=lookups, save=saved.append)
    assert asked == [] and again == matched
