"""discover-pages 的单商家发现：站点地图与站内搜索得到候选页，抓取并按价格证据评估，促销中心页跟一跳。"""
from __future__ import annotations

import argparse
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

from utils.crawler_visibility_fetch import fetch_stylesheets
from utils.discovery_search import BudgetExhausted
from utils.ego_browser import ego_scrape
from utils.firecrawl_client import SelfHostedFirecrawl, firecrawl_session
from utils.promo_page_discovery import (
    assess_offer_page,
    candidate_urls,
    extract_end_dates,
    hub_links,
    is_hub_page,
    search_candidates,
)
from utils.topic_search import site_root


class Firecrawl:
    def __init__(self, api_url: str, api_key: str = "", timeout: float = 120, search_interval: float = 0.0) -> None:
        self.search_interval = search_interval
        self._search_lock = threading.Lock()
        self._last_search = 0.0
        self.api_url = api_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout
        self.session = firecrawl_session()
        # 单页抓取统一经 SelfHostedFirecrawl：先 fastMode，拿不到正文时由 ego-lite 渲染
        self.pages = SelfHostedFirecrawl(api_url, api_key, timeout, browser=ego_scrape)

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        response = self.session.post(f"{self.api_url}{path}", headers=self.headers, json=body, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def map(self, url: str, limit: int) -> List[str]:
        # /v2/map 只读站点地图与页面链接，不经过 Firecrawl 的浏览器引擎；links 是 {url, title, description}
        payload = self._post("/v2/map", {"url": url, "limit": limit})
        return [str(link["url"]) for link in payload.get("links") or []]

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._search_lock:  # one search at a time, spaced out, so upstream engines are not burst
            wait = self.search_interval - (time.monotonic() - self._last_search)
            if wait > 0:
                time.sleep(wait)
            self._last_search = time.monotonic()
        payload = self._post("/v1/search", {"query": query, "limit": limit})
        return [r for r in payload.get("data") or [] if isinstance(r, dict)]

    def links(self, url: str) -> List[str]:
        data = self.pages.scrape(url, ("links",), only_main_content=False)
        return [str(link) for link in data.get("links") or []]

    def scrape(self, url: str) -> Dict[str, str]:
        data = self.pages.scrape(url, ("markdown", "rawHtml"), only_main_content=False)
        return {"markdown": str(data.get("markdown") or ""), "rawHtml": str(data.get("rawHtml") or "")}


def discover_per_site(rows: List[Dict[str, Any]], discover: Callable[[Dict[str, Any]], Dict[str, Any]],
                      map_fn: Callable = map) -> List[Dict[str, Any]]:
    """连锁品牌的分店共用同一个网站：每个站点只发现一次，结果复制给该站点的每家分店（按 rows 原顺序返回）。"""
    def site(row: Dict[str, Any]) -> str:
        return site_root(row["website"].strip())

    sites: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        sites.setdefault(site(row), []).append(row)
    firsts = dict(zip(sites, map_fn(lambda group: discover(group[0]), sites.values())))
    results = []
    for row in rows:
        lead = sites[site(row)][0]
        results.append(firsts[site(row)] if row is lead else
                       {**firsts[site(row)], "business_id": row["business_id"], "name": row.get("name"),
                        "city": row.get("city"), "website": row.get("website"), "shared_site_with": lead["business_id"]})
    return results


# 同一次运行内重跑的商家：出错，或站内搜索遇到搜索引擎不可用；云端额度用尽重跑无益，留给下一次运行
RERUN_SEARCH_STATUSES = frozenset({"skipped_engines_down", "error"})
RERUN_ROUNDS = 3
RERUN_PAUSE_S = 120.0


def needs_rerun(result: Dict[str, Any]) -> bool:
    return bool(result.get("error")) or result.get("search_status") in RERUN_SEARCH_STATUSES


def finished(result: Dict[str, Any]) -> bool:
    """已完成的商家：再次运行同一批次时不重复发现。"""
    return not needs_rerun(result) and result.get("search_status") != "skipped_budget"


def discover_until_stable(rows: List[Dict[str, Any]], run_round: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
                          *, rounds: int = RERUN_ROUNDS, pause: float = RERUN_PAUSE_S,
                          sleep: Callable[[float], None] = time.sleep, reset: Callable[[], None] = lambda: None
                          ) -> List[Dict[str, Any]]:
    """先发现全部商家，再对需要重跑的商家按递增间隔重跑；没有待重跑的商家，或一轮没有任何商家恢复时停止。
    reset 在每轮重跑前调用（重新探测搜索引擎）。"""
    results = {r["business_id"]: r for r in run_round(rows)}
    for attempt in range(1, rounds + 1):
        pending = [row for row in rows if needs_rerun(results[row["business_id"]])]
        if not pending:
            break
        sleep(pause * attempt)
        reset()
        rerun = run_round(pending)
        results.update({r["business_id"]: r for r in rerun})
        if all(needs_rerun(r) for r in rerun):
            break
    return [results[row["business_id"]] for row in rows]


def merge_results(earlier: List[Dict[str, Any]], fresh: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """本次结果覆盖同一商家的早先结果，其余早先结果保留；按 business_id 排序。"""
    merged = {r["business_id"]: r for r in earlier}
    merged.update({r["business_id"]: r for r in fresh})
    return [merged[key] for key in sorted(merged)]


def select_discovery_businesses(rows: List[Dict[str, Any]], business_ids: Set[int],
                                seeds: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if business_ids:
        return [row for row in rows if row["business_id"] in business_ids]
    if seeds is not None:
        return [row for row in rows if site_root(row["website"]) in seeds]
    return rows


def discover_business(fc: Firecrawl, business: Dict[str, Any], known: Set[str], args: argparse.Namespace) -> Dict[str, Any]:
    result: Dict[str, Any] = {"business_id": business["business_id"], "name": business.get("name"),
                              "city": business.get("city"), "website": business.get("website"),
                              "mapped": 0, "candidates": 0, "pages": [], "error": None}
    try:
        sources = {s.strip() for s in args.sources.split(",") if s.strip()}
        mapped = fc.map(business["website"], args.map_limit) if "map" in sources else []
        result["mapped"] = len(mapped)
        if "map" in sources and len(mapped) <= 3:  # map saw only the home page: fall back to its rendered links
            mapped = list(dict.fromkeys(mapped + fc.links(business["website"])))
            result["map_fallback_links"] = len(mapped)
        candidates = candidate_urls(business["website"], mapped, known=known, allow_blog=args.allow_blog,
                                    limit=args.max_candidates)
        for c in candidates:
            c["sources"] = ["map"]
        # topic-search 命中的同站促销类页面排在最前，不受 map 候选数量上限挤占
        seeds = getattr(args, "seeds", {}).get(site_root(business["website"]), [])
        by_url = {s["url"]: {"url": s["url"], "kind_hint": s.get("kind_hint") or "promotion", "title": s.get("title", ""),
                             "sources": ["topic_search"]} for s in seeds if s["url"] not in known}
        for c in candidates:
            if c["url"] in by_url:
                by_url[c["url"]]["sources"].append("map")
            else:
                by_url[c["url"]] = c
        if "search" in sources:
            host = urlparse(business["website"] if "://" in business["website"] else "https://" + business["website"]).netloc
            host = host[4:] if host.startswith("www.") else host
            query = f"site:{host} (special OR promotion OR offer OR deal OR membership OR package OR pricing)"
            found = []
            raw: List[Dict[str, Any]] = []
            health = getattr(args, "search_health", None)
            try:
                if health is not None and health.down:
                    result["search_status"] = "skipped_engines_down"
                    with health.lock:
                        health.skipped += 1
                else:
                    searcher = getattr(args, "search_client", None) or fc
                    raw = searcher.search(query, args.search_limit)
                    cloud = getattr(args, "search_client", None) is not None
                    if not raw and not cloud and getattr(args, "search_retry_delay", 0):
                        time.sleep(args.search_retry_delay)  # self-hosted: empty may mean throttled engines
                        raw = searcher.search(query, args.search_limit)
                    if health is not None:
                        health.record(len(raw))
                        if not raw and health.empty_streak >= health.empty_limit and not health.down:
                            time.sleep(health.cooldown)
                            canary = fc.search(health.CANARY, 5)
                            if not canary:
                                health.down = True
                            else:
                                health.record(len(canary))
                    result["search_status"] = "ok" if raw else ("skipped_engines_down" if health and health.down
                                                                  else "no_results")
                    found = search_candidates(business["website"], raw, known=known, allow_blog=args.allow_blog,
                                              limit=args.max_candidates)
            except BudgetExhausted as exc:
                result["search_status"] = "skipped_budget"
                result["search_error"] = str(exc)[:120]
            except Exception as exc:
                result["search_status"] = "error"
                result["search_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            result["search_results"] = len(raw)
            result["searched"] = len(found)
            for c in found:
                if c["url"] in by_url:
                    by_url[c["url"]]["sources"].append("search")
                    by_url[c["url"]]["title"] = c.get("title", "")
                else:
                    by_url[c["url"]] = {**c, "sources": ["search"]}
        candidates = list(by_url.values())[: max(args.max_candidates, 1) * 2]
        result["candidates"] = len(candidates)
        visited: Set[str] = {c["url"] for c in candidates}
        sources_of = {c["url"]: c.get("sources", []) for c in candidates}
        titles_of = {c["url"]: c.get("title", "") for c in candidates}
        budget = args.max_pages

        def assess(url: str, kind_hint: str, hop: int, via: Optional[str]) -> Optional[Dict[str, Any]]:
            nonlocal budget
            if budget <= 0:
                return None
            budget -= 1
            try:
                page = fc.scrape(url)
            except Exception as exc:  # one failing page must not stop the site
                result["pages"].append({"url": url, "hop": hop, "via": via, "status": "scrape_failed",
                                        "kind_hint": kind_hint, "sources": sources_of.get(url, []),
                                        "title": titles_of.get(url, ""), "error": str(exc)[:160]})
                return None
            assessment = assess_offer_page(page["markdown"], page["rawHtml"], kind_hint=kind_hint, page_url=url,
                                           stylesheets=fetch_stylesheets(url, page["rawHtml"]))
            result["pages"].append({
                "url": url, "hop": hop, "via": via, "kind_hint": kind_hint, "kind": assessment.kind,
                "sources": sources_of.get(url, []), "title": titles_of.get(url, ""),
                "end_dates": extract_end_dates(page["markdown"]),
                "status": ("needs_review" if assessment.needs_review else "valid") if assessment.valid else "rejected",
                "evidence": list(assessment.evidence), "reasons": list(assessment.reasons),
            })
            if args.hops >= 1 and hop == 0 and is_hub_page(assessment, kind_hint):
                return {"rawHtml": page["rawHtml"]}
            return None

        hubs = []
        for candidate in candidates:
            hub = assess(candidate["url"], candidate["kind_hint"], 0, None)
            if hub:
                hubs.append((candidate, hub))
        for candidate, hub in hubs:
            for link in hub_links(hub["rawHtml"], candidate["url"], known=known, visited=visited,
                                  limit=args.max_hop_links):
                visited.add(link)
                assess(link, candidate["kind_hint"], 1, candidate["url"])
            result["hubs"] = result.get("hubs", 0) + 1
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return result
