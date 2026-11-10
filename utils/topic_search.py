"""按城市、服务主题搜索促销页面：把搜索命中按网站归到 Google Maps 查到的诊所，产出待注册的诊所和每家诊所的候选页面。

搜索结果里混着百科、点评、团购、厂商站点和外地诊所。只有网站域名与目标区域某家诊所（Google Maps 结果）一致的命中才保留，
其余按「没有对应诊所」统计；同一域名可以对应多家分店，每家分店都注册。
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from utils.firecrawl_scrape_raw_db import canonical_scrape_url
from utils.promo_page_discovery import is_aggregator, registrable_root, search_candidates

DEFAULT_QUERY_TEMPLATES = ("botox specials {area}", "botox promotion {area} med spa", "botox $ per unit special {area}")
# Google Maps 类别明显不是医美诊所的地点：同域名的命中不算诊所（例如普拉提馆、理发店、公司总部）
NON_CLINIC_CATEGORIES = frozenset({
    "physical fitness program", "gym", "yoga studio", "pilates studio", "hair salon", "barber shop", "nail salon",
    "corporate office", "software company", "marketing agency", "pharmacy",
})


def expand_queries(templates: Iterable[str], areas: Iterable[str]) -> list[str]:
    return [template.format(area=area) for area in areas for template in templates]


def site_root(url: str) -> str:
    host = urlsplit(url if "://" in url else "https://" + url).hostname or ""
    return registrable_root(host.lower())


def run_topic_search(search: Callable[[str, int], list[Mapping[str, Any]]], queries: Iterable[str], *,
                     limit: int, interval: float) -> list[dict[str, str]]:
    """逐条查询；interval 是两次查询之间的最短间隔（自托管搜索的上游引擎会限制连续请求）。"""
    hits: list[dict[str, str]] = []
    for index, query in enumerate(queries):
        if index:
            time.sleep(interval)
        for row in search(query, limit):
            url = canonical_scrape_url(str(row.get("url") or ""))
            if url:
                hits.append({"query": query, "url": url, "title": str(row.get("title") or ""),
                             "description": str(row.get("description") or "")})
    return hits


def match_hits(hits: list[Mapping[str, str]], places: list[Mapping[str, Any]],
               cities: Iterable[str]) -> dict[str, Any]:
    """命中按网站域名归到目标城市的诊所；返回 clinics（含候选页面）、out_of_area 与 unmatched 三类。"""
    wanted = {city.casefold() for city in cities}
    places_by_root: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for place in places:
        if (str(place.get("website") or "").strip()
                and str(place.get("category") or "").casefold() not in NON_CLINIC_CATEGORIES):
            places_by_root[site_root(str(place["website"]))].append(place)
    hits_by_root: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for hit in hits:
        root = site_root(hit["url"])
        if root and not is_aggregator(root):
            hits_by_root[root].append(hit)
    clinics, out_of_area, unmatched = [], [], []
    for root, root_hits in sorted(hits_by_root.items()):
        local = [p for p in places_by_root.get(root, []) if str(p.get("city") or "").casefold() in wanted]
        if local:
            website = str(local[0]["website"])
            candidates = search_candidates(website, root_hits, limit=50)
            queries = {hit["url"]: hit["query"] for hit in root_hits}
            clinics.append({"root": root, "website": website, "places": local, "hits": root_hits,
                            "candidates": [{**c, "query": queries.get(c["url"], "")} for c in candidates]})
        elif places_by_root.get(root):
            out_of_area.append({"root": root, "cities": sorted({str(p.get("city")) for p in places_by_root[root]}),
                                "hits": root_hits})
        else:
            unmatched.append({"root": root, "hits": root_hits})
    return {"clinics": clinics, "out_of_area": out_of_area, "unmatched": unmatched}


def lookup_keyword(root: str, city: str, state: str) -> str:
    """Google Maps 补查关键词：网站域名主体加城市与州。"""
    return f"{root.split('.')[0]} {city}, {state}"


def match_with_lookups(hits: list[Mapping[str, str]], places: list[Mapping[str, Any]], cities: Iterable[str],
                       lookup: Callable[[str], list[dict[str, Any]]], *, done: Mapping[str, list[dict[str, Any]]],
                       save: Callable[[dict[str, list[dict[str, Any]]]], None]
                       ) -> tuple[dict[str, Any], list[Mapping[str, Any]], dict[str, list[dict[str, Any]]]]:
    """匹配命中；没有对应诊所的站点逐个在 Google Maps 补查，补查到的地点加入后重新匹配，直至没有未查过的站点。
    done 是早先的补查结果（不再重复查询），每查完一个站点调用 save 保存全部补查结果。
    返回 (匹配结果, 全部地点, 全部补查结果)。"""
    lookups = dict(done)
    while True:
        everything = list({str(p["google_place_id"]): p for p in
                           [*places, *(p for found in lookups.values() for p in found)]}.values())
        matched = match_hits(hits, everything, cities)
        pending = [site["root"] for site in matched["unmatched"] if site["root"] not in lookups]
        if not pending:
            return matched, everything, lookups
        for root in pending:
            lookups[root] = lookup(root)
            save(dict(lookups))


def search_report(hits: list[Mapping[str, str]], matched: Mapping[str, Any]) -> dict[str, Any]:
    """搜索发现的精度：命中里有多少落在目标区域的诊所网站上，其中多少本身就是促销类页面。"""
    clinic_hits = sum(len(c["hits"]) for c in matched["clinics"])
    return {
        "hits": len(hits),
        "sites": len(matched["clinics"]) + len(matched["out_of_area"]) + len(matched["unmatched"]),
        "clinic_sites": len(matched["clinics"]),
        "clinic_places": sum(len(c["places"]) for c in matched["clinics"]),
        "clinic_hits": clinic_hits,
        "clinic_hit_rate": round(clinic_hits / len(hits), 3) if hits else 0.0,
        "promotion_page_candidates": sum(len(c["candidates"]) for c in matched["clinics"]),
        "out_of_area_sites": [c["root"] for c in matched["out_of_area"]],
        "unmatched_sites": len(matched["unmatched"]),
    }
