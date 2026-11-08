#!/usr/bin/env python3
"""Discover new promotion / membership pages with the self-hosted Firecrawl Map endpoint.

For each business website: map the site, keep same-site URLs whose path suggests a promotion,
membership or price page and that are not already monitored or stored, scrape them, and keep only
pages that state a real discount or price visible on the page. Read-only against the database;
results are written to a JSON file for review (nothing is added to the monitor automatically).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Callable, Dict, List, Optional, Set

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

from utils.ego_browser import ego_scrape  # noqa: E402
from utils.firecrawl_client import SelfHostedFirecrawl, firecrawl_session  # noqa: E402
from utils.firecrawl_scrape_raw_db import canonical_scrape_url  # noqa: E402
from utils.promo_page_discovery import (  # noqa: E402
    assess_offer_page,
    candidate_urls,
    extract_end_dates,
    hub_links,
    is_hub_page,
    search_candidates,
)
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402
from utils.topic_search import site_root  # noqa: E402



class SearchHealth:
    """Tracks whether the search backend answers. An empty result is ambiguous (no hits, or the
    upstream engines are throttled); a canary query decides, and a down backend is not hammered."""

    CANARY = "wikipedia"

    def __init__(self, empty_limit: int = 6, cooldown: float = 90.0) -> None:
        self.lock = threading.Lock()
        self.empty_streak = 0
        self.down = False
        self.skipped = 0
        self.empty_limit = empty_limit
        self.cooldown = cooldown

    def record(self, results: int) -> None:
        with self.lock:
            self.empty_streak = 0 if results else self.empty_streak + 1


class BudgetExhausted(RuntimeError):
    pass


class CloudSearch:
    """Firecrawl cloud ``/v2/search`` with a hard credit budget (each search costs 2 credits, any limit)."""

    COST = 2

    def __init__(self, api_key: str, *, budget: int, reserve: int = 150, interval: float = 15.0,
                 session: Optional[requests.Session] = None, api_url: str = "https://api.firecrawl.dev") -> None:
        self.api_url = api_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.interval = interval  # the plan allows about 4 requests a minute
        self.reserve = reserve
        self.budget = budget
        self.spent = 0
        self.searches = 0
        self._lock = threading.Lock()
        self._last = 0.0

    def remaining_credits(self) -> int:
        for attempt in range(4):
            try:
                r = self.session.get(f"{self.api_url}/v2/team/credit-usage", headers=self.headers, timeout=60)
                r.raise_for_status()
                return int(r.json()["data"]["remainingCredits"])
            except requests.exceptions.SSLError:
                time.sleep(3 * (attempt + 1))
        raise RuntimeError("credit_usage_unavailable")

    def effective_budget(self) -> int:
        """Never spend into the reserve, whatever --credit-budget says."""
        return max(0, min(self.budget, self.remaining_credits() - self.reserve))

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            if self.spent + self.COST > self.budget:
                raise BudgetExhausted(f"spent {self.spent} of {self.budget} credits")
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            body = {"query": query, "limit": min(limit, 10)}
            response = None
            for attempt in range(5):
                try:
                    response = self.session.post(f"{self.api_url}/v2/search", headers=self.headers, json=body, timeout=90)
                except requests.exceptions.SSLError:
                    time.sleep(3 * (attempt + 1))
                    continue
                if response.status_code == 429:
                    time.sleep(float(response.json().get("retry_after_seconds", 30)) + 2)
                    continue
                break
            self._last = time.monotonic()
            if response is None:
                raise RuntimeError("cloud_search_unreachable")
            if response.status_code == 402:
                self.budget = self.spent  # out of credits: stop spending
                raise BudgetExhausted("insufficient credits")
            response.raise_for_status()
            payload = response.json()
            self.spent += int(payload.get("creditsUsed") or self.COST)
            self.searches += 1
        data = payload.get("data")
        items = data.get("web") if isinstance(data, dict) else data
        return [i for i in items or [] if isinstance(i, dict)]


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


def known_urls(client: Any) -> Set[str]:
    known: Set[str] = set()
    for table, pk in (("clinic_promotions", "promotion_id"), ("clinic_services", "service_id"),
                      ("clinic_memberships", "plan_id")):
        for row in fetch_all_rows(client, table, f"{pk},source_url", order=f"{pk}.asc"):
            if row.get("source_url"):
                known.add(canonical_scrape_url(row["source_url"]))
    known.discard("")
    return known


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
                                        "error": str(exc)[:160]})
                return None
            assessment = assess_offer_page(page["markdown"], page["rawHtml"], kind_hint=kind_hint)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", type=int, action="append")
    parser.add_argument("--city")
    parser.add_argument("--limit", type=int, default=20, help="max businesses to process")
    parser.add_argument("--after-business-id", type=int, default=0)
    parser.add_argument("--map-limit", type=int, default=500)
    parser.add_argument("--max-candidates", type=int, default=8)
    parser.add_argument("--allow-blog", action="store_true")
    parser.add_argument("--sources", default="map,search",
                        help="comma-separated: map (/v2/map), search (site-restricted /v1/search)")
    parser.add_argument("--search-limit", type=int, default=10)
    parser.add_argument("--search-backend", choices=("self", "cloud"), default="self",
                        help="cloud = api.firecrawl.dev /v2/search (2 credits per search); candidates are still "
                             "scraped on the self-hosted instance")
    parser.add_argument("--credit-budget", type=int, default=300, help="cloud: max credits this run may spend")
    parser.add_argument("--credit-reserve", type=int, default=150, help="cloud: credits that must remain afterwards")
    parser.add_argument("--uncovered-from", type=Path,
                        help="earlier discovery output: only businesses without a valid page there; "
                             "never-processed businesses first")
    parser.add_argument("--search-interval", type=float, default=4.0,
                        help="minimum seconds between search requests (SearXNG upstream engines throttle bursts)")
    parser.add_argument("--search-retry-delay", type=float, default=15.0, help="pause before retrying an empty search")
    parser.add_argument("--resume-from", type=Path,
                        help="earlier output: skip businesses whose search already returned results")
    parser.add_argument("--retry-status", action="append",
                        help="with --resume-from: only businesses whose earlier search_status is this value "
                             "(for example skipped_budget); repeatable")
    parser.add_argument("--hops", type=int, default=1, choices=(0, 1),
                        help="1 = follow offers one click from hub pages that state no price themselves")
    parser.add_argument("--max-hop-links", type=int, default=8, help="links followed per hub page")
    parser.add_argument("--max-pages", type=int, default=24, help="page budget per business")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seeds", type=Path,
                        help="topic-search seeds.json: candidate pages per site; without --business-id only these sites run")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "state" / "discovery" / "new_promo_pages.json")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    api_url = os.getenv("FIRECRAWL_SELF_HOSTED_API_URL", "http://127.0.0.1:3002").strip("'\"")
    fc = Firecrawl(api_url, os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY", ""), search_interval=args.search_interval)
    args.search_health = SearchHealth()
    args.search_client = None
    if args.search_backend == "cloud":
        import re as _re

        keys = _re.findall(r"^FIRECRAWL_API_KEY=\s*['\"]?(fc-[A-Za-z0-9]+)", (PROJECT_ROOT / ".env").read_text(), _re.M)
        if not keys:
            raise SystemExit("no FIRECRAWL_API_KEY in .env")
        # .env holds two keys; the first is the account that still has credits (checked below)
        client_cloud = CloudSearch(keys[0], budget=args.credit_budget, reserve=args.credit_reserve)
        remaining = client_cloud.remaining_credits()
        client_cloud.budget = client_cloud.effective_budget()
        print(f"cloud search: {remaining} credits remaining, budget this run {client_cloud.budget} "
              f"(~{client_cloud.budget // CloudSearch.COST} searches), reserve {args.credit_reserve}", flush=True)
        args.search_client = client_cloud

    client = load_supabase_client(PROJECT_ROOT, role="service")
    rows = fetch_all_rows(client, "master_business_info", "business_id,name,city,website", order="business_id.asc")
    rows = [r for r in rows if (r.get("website") or "").strip() and r["business_id"] > args.after_business_id]
    seeds = ({root: site["candidates"] for root, site in json.loads(args.seeds.read_text()).items()}
             if args.seeds is not None else None)
    rows = select_discovery_businesses(rows, set(args.business_id or []), seeds)
    args.seeds = seeds or {}
    if args.city:
        rows = [r for r in rows if (r.get("city") or "").lower() == args.city.lower()]
    if args.uncovered_from and args.uncovered_from.exists():
        covered = set()
        for b in json.loads(args.uncovered_from.read_text())["businesses"]:
            if any(p.get("status") in ("valid", "needs_review") for p in b.get("pages", [])):
                covered.add(b["business_id"])
        from scripts.review_promo_candidates import load_businesses  # noqa: E402

        context = load_businesses()
        rows = [r for r in rows if r["business_id"] not in covered]
        rows.sort(key=lambda r: (not (context.get(r["business_id"]) and context[r["business_id"]].never_processed),
                                 r["business_id"]))
    if args.resume_from and args.resume_from.exists():
        earlier = json.loads(args.resume_from.read_text())["businesses"]
        if args.retry_status:
            wanted = {b["business_id"] for b in earlier if b.get("search_status") in set(args.retry_status)}
            rows = [r for r in rows if r["business_id"] in wanted]
        else:
            done = {b["business_id"] for b in earlier if b.get("search_status") == "ok" or b.get("searched", 0) > 0}
            rows = [r for r in rows if r["business_id"] not in done]
    rows = rows[: args.limit]
    known = known_urls(client)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = discover_per_site(rows, lambda b: discover_business(fc, b, known, args), pool.map)

    pages = [p | {"business_id": r["business_id"], "business": r["name"]} for r in results for p in r["pages"]]
    summary = {
        "businesses": len(results), "errors": sum(1 for r in results if r["error"]),
        "mapped_urls": sum(r["mapped"] for r in results), "candidates": sum(r["candidates"] for r in results),
        "from_search": sum(1 for p in pages if "search" in (p.get("sources") or [])),
        "search_ok": sum(1 for r in results if r.get("search_status") == "ok"),
        "search_no_results": sum(1 for r in results if r.get("search_status") == "no_results"),
        "search_skipped_engines_down": sum(1 for r in results if r.get("search_status") == "skipped_engines_down"),
        "search_engines_down": bool(args.search_health.down),
        "search_skipped_budget": sum(1 for r in results if r.get("search_status") == "skipped_budget"),
        "cloud_credits_spent": getattr(args.search_client, "spent", 0),
        "cloud_searches": getattr(args.search_client, "searches", 0),
        "hub_pages": sum(r.get("hubs", 0) for r in results),
        "hop_pages": sum(1 for p in pages if p.get("hop") == 1),
        "hop_valid": sum(1 for p in pages if p.get("hop") == 1 and p["status"] in ("valid", "needs_review")),
        "valid": sum(1 for p in pages if p["status"] == "valid"),
        "needs_review": sum(1 for p in pages if p["status"] == "needs_review"),
        "rejected": sum(1 for p in pages if p["status"] == "rejected"),
        "scrape_failed": sum(1 for p in pages if p["status"] == "scrape_failed"),
        "elapsed_s": round(time.monotonic() - started),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "summary": summary,
                                    "businesses": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
