"""discover-pages 结束前重试抓取失败的页面：低并发、退避重试自托管 Firecrawl，仍失败时退回有边界的公开 HTTP 抓取，
重新评估后替换原来的 scrape_failed 记录。不使用云端额度。"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

from utils.direct_public_scrape import DEFAULT_HEADERS, html_to_text
from utils.promo_page_discovery import assess_offer_page, extract_end_dates
from utils.url_safety import assert_safe_crawl_entry_url


def collect_failed(results: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """各商家结果中抓取失败的网址（去掉在别处抓取成功的）。连锁分店共用同一网站时同一网址只重试一次，结果写给每家分店。"""
    failed: Dict[str, Dict[str, Any]] = {}
    good: set = set()
    for business in results:
        for page in business.get("pages", []):
            url = page["url"]
            if page.get("status") == "scrape_failed":
                record = failed.setdefault(url, {
                    "businesses": [], "kind_hint": page.get("kind_hint") or "promotion", "sources": [],
                    "title": page.get("title", ""), "hop": page.get("hop", 0), "via": page.get("via"),
                    "error": page.get("error"),
                })
                if business["business_id"] not in record["businesses"]:
                    record["businesses"].append(business["business_id"])
                for source in page.get("sources") or []:
                    if source not in record["sources"]:
                        record["sources"].append(source)
            else:
                good.add(url)
    return {u: r for u, r in failed.items() if u not in good}


def direct_fetch(url: str, timeout: float = 20) -> Dict[str, str]:
    """Bounded public fetch: no access controls bypassed, SSRF-guarded, HTML documents only."""
    safe = assert_safe_crawl_entry_url(url)
    response = requests.get(safe, headers=DEFAULT_HEADERS, timeout=timeout)
    response.raise_for_status()
    if "html" not in response.headers.get("Content-Type", "").casefold():
        raise ValueError("unsupported_content_type")
    html = response.text
    return {"markdown": html_to_text(html), "rawHtml": html}


def retry_one(
    fc: Any, url: str, record: Dict[str, Any], *, attempts: int = 3, backoff: float = 5.0,
    fetch: Callable[[str], Dict[str, str]] = direct_fetch, sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    page: Optional[Dict[str, str]] = None
    engine, last_error = "", ""
    for attempt in range(attempts):
        try:
            page, engine = fc.scrape(url), "firecrawl"
            if page["markdown"].strip() or page["rawHtml"].strip():
                break
            page, last_error = None, "empty_page"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
        if attempt < attempts - 1:
            sleep(backoff * (3 ** attempt))
    if page is None:
        try:
            page, engine = fetch(url), "direct_http"
        except Exception as exc:
            return {"url": url, "hop": record["hop"], "via": record["via"], "status": "scrape_failed",
                    "kind_hint": record["kind_hint"], "sources": record["sources"], "title": record["title"],
                    "error": f"{last_error}; direct: {type(exc).__name__}: {str(exc)[:100]}", "retried": True}
    assessment = assess_offer_page(page["markdown"], page["rawHtml"], kind_hint=record["kind_hint"])
    return {
        "url": url, "hop": record["hop"], "via": record["via"], "kind_hint": record["kind_hint"],
        "kind": assessment.kind, "sources": record["sources"], "title": record["title"],
        "end_dates": extract_end_dates(page["markdown"]), "retried": True, "engine": engine,
        "status": ("needs_review" if assessment.needs_review else "valid") if assessment.valid else "rejected",
        "evidence": list(assessment.evidence), "reasons": list(assessment.reasons),
    }


def retry_failed_pages(results: List[Dict[str, Any]], retry: Callable[[str, Dict[str, Any]], Dict[str, Any]],
                       workers: int = 2) -> Dict[str, int]:
    """重试全部抓取失败的网址，用重试结果原位替换每家商家的 scrape_failed 记录；返回各状态的网址数。"""
    failed = collect_failed(results)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        retried = dict(zip(failed, pool.map(lambda item: retry(*item), failed.items())))
    for business in results:
        business["pages"] = [retried.get(page["url"], page) if page.get("status") == "scrape_failed" else page
                             for page in business.get("pages", [])]
    counts: Dict[str, int] = {"urls": len(retried)}
    for page in retried.values():
        counts[page["status"]] = counts.get(page["status"], 0) + 1
        if page.get("engine"):
            counts[f"recovered_by_{page['engine']}"] = counts.get(f"recovered_by_{page['engine']}", 0) + 1
    return counts
