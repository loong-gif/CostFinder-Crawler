"""Firecrawl Search orchestration for clinic_services price discovery."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

from firecrawl.v2.types import ScrapeOptions

from config.settings import CLINIC_SERVICES_SEARCH_LIMIT, CLINIC_SERVICES_SEARCH_TIMEOUT
from utils.clinic_services_botox import website_to_crawl_url
from utils.firecrawl_client import get_firecrawl_search_client
from utils.firecrawl_search_raw_db import save_search_queries, search_web_row
from utils.scrape_markdown import prepare_scrape_markdown

SEARCH_QUERIES: dict[str, list[str]] = {
    "Botox": [
        'botox ("per unit" OR "/unit" OR "unit price")',
        "botox pricing injectables",
    ],
}

_EXCLUDE_PATH_RE = re.compile(
    r"/(?:specials?|promos?|promotions?|deals?|membership|blog|book(?:ing)?|appointment)s?(?:/|$)",
    re.IGNORECASE,
)
_PREFER_PATH_RE = re.compile(
    r"/(?:services?|pricing|menu|injectables?|treatments?|aesthetics?)(?:/|$)",
    re.IGNORECASE,
)
_ARTICLE_PATH_RE = re.compile(
    r"/(?:blog|news|articles?|posts?)(?:/|$)"
    r"|before-and-after|results-timeline|what-is-|how-to-|guide-to-|vs-|faq",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SearchPage:
    url: str
    title: str
    markdown: str


def business_base_domain(website: Any) -> str:
    """Return host for include_domains (full host, not registrable root)."""
    crawl_url = website_to_crawl_url(website)
    if not crawl_url:
        return ""
    host = (urlparse(crawl_url).netloc or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def _normalize_host(url: str) -> str:
    host = (urlparse(url).netloc or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def host_matches_domain(url: str, domain: str) -> bool:
    host = _normalize_host(url)
    domain = domain.lower().strip()
    if not host or not domain:
        return False
    return host == domain or host.endswith("." + domain)


def url_path_score(url: str) -> int:
    path = urlparse(url).path or ""
    if _EXCLUDE_PATH_RE.search(path):
        return -100
    if _PREFER_PATH_RE.search(path):
        return 30
    if _ARTICLE_PATH_RE.search(path):
        return -40
    slug = path.strip("/").split("/")[-1] if path.strip("/") else ""
    if slug.count("-") >= 5:
        return -20
    return 0


def is_article_service_url(url: str) -> bool:
    return url_path_score(url) < 0


def search_hit_text(hit: dict[str, Any]) -> str:
    parts = [
        str(hit.get("title") or "").strip(),
        str(hit.get("description") or "").strip(),
        str(hit.get("markdown") or hit.get("content") or "").strip(),
    ]
    return "\n".join(part for part in parts if part)


def pick_service_search_hit(
    hits: Sequence[dict[str, Any]],
    *,
    domain: str,
) -> dict[str, Any] | None:
    """Pick the best menu/pricing page for a domain from Search API hits."""
    ranked: list[tuple[int, int, str, dict[str, Any]]] = []
    for hit in hits:
        url = str(hit.get("url") or "").strip()
        if not url or not host_matches_domain(url, domain):
            continue
        score = url_path_score(url)
        if score < 0:
            continue
        position_raw = hit.get("position")
        try:
            position = int(position_raw)
        except (TypeError, ValueError):
            position = 999
        ranked.append((score, -position, url, hit))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return ranked[0][3]


def filter_service_menu_urls(pages: Sequence[SearchPage], *, domain: str) -> List[SearchPage]:
    """Drop promo/blog URLs; prefer pricing/services paths."""
    kept = [p for p in pages if host_matches_domain(p.url, domain)]
    kept = [p for p in kept if url_path_score(p.url) >= 0]
    kept.sort(key=lambda p: (-url_path_score(p.url), p.url))
    return kept


def _item_to_search_page(item: Any) -> Optional[SearchPage]:
    url = str(getattr(item, "url", None) or getattr(item, "metadata", None) and getattr(item.metadata, "source_url", None) or "").strip()
    if not url and isinstance(item, dict):
        url = str(item.get("url") or (item.get("metadata") or {}).get("sourceURL") or "").strip()
    markdown = str(getattr(item, "markdown", None) or "")
    if not markdown and isinstance(item, dict):
        markdown = str(item.get("markdown") or item.get("content") or "")
    title = str(getattr(item, "title", None) or "")
    if not title and isinstance(item, dict):
        title = str(item.get("title") or "")
    if not url:
        return None
    return SearchPage(url=url, title=title, markdown=prepare_scrape_markdown(markdown))


def _search_once(
    fc: Any,
    query: str,
    *,
    domain: str,
    limit: int,
    timeout: int,
) -> List[SearchPage]:
    result = fc.search(
        query,
        include_domains=[domain],
        limit=limit,
        timeout=timeout,
        scrape_options=ScrapeOptions(
            formats=["markdown"],
            only_main_content=True,
            block_ads=True,
        ),
    )
    web = getattr(result, "web", None) or []
    pages: List[SearchPage] = []
    for item in web:
        page = _item_to_search_page(item)
        if page and page.markdown:
            pages.append(page)
    return pages


def search_service_pages(
    website: Any,
    service_name: str,
    *,
    limit: Optional[int] = None,
    timeout: Optional[int] = None,
    max_retries: int = 2,
    client: Any = None,
) -> tuple[List[SearchPage], List[str]]:
    """Run configured queries for service; return filtered pages and queries used."""
    domain = business_base_domain(website)
    if not domain:
        return [], []
    queries = SEARCH_QUERIES.get(service_name, [])
    if not queries:
        raise ValueError(f"No search queries configured for service_name={service_name!r}")

    fc = get_firecrawl_search_client()
    per_query_limit = limit if limit is not None else CLINIC_SERVICES_SEARCH_LIMIT
    per_query_timeout = timeout if timeout is not None else CLINIC_SERVICES_SEARCH_TIMEOUT

    seen_urls: set[str] = set()
    merged: List[SearchPage] = []
    for query in queries:
        last_err: Optional[Exception] = None
        for attempt in range(max_retries + 1):
            try:
                batch = _search_once(
                    fc,
                    query,
                    domain=domain,
                    limit=per_query_limit,
                    timeout=per_query_timeout,
                )
                for page in batch:
                    if page.url in seen_urls:
                        continue
                    seen_urls.add(page.url)
                    merged.append(page)
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                err_text = str(exc).lower()
                if "429" in err_text or "rate" in err_text:
                    wait = 5 * (attempt + 1)
                    time.sleep(wait)
                    continue
                raise
        if last_err is not None:
            raise last_err

    return filter_service_menu_urls(merged, domain=domain), list(queries)


def persist_search_raw(
    client: Any,
    *,
    website: Any,
    service_name: str,
    query_pages: dict[str, Sequence[SearchPage]],
    success: bool = True,
    error_message: Optional[str] = None,
) -> dict[str, int]:
    """Save one firecrawl_search_raw row per search_query."""
    domain = business_base_domain(str(website))
    entries: list[tuple[str, list[dict[str, Any]]]] = []
    for query, pages in query_pages.items():
        rows = [
            row
            for page in pages
            if (row := search_web_row({"url": page.url, "title": page.title, "markdown": page.markdown}))
        ]
        if rows:
            entries.append((query, rows))
    return save_search_queries(
        client,
        website=str(website),
        domain=domain,
        entries=entries,
        success=success,
        error_message=error_message,
    )


def search_pages_to_dicts(pages: Iterable[SearchPage]) -> List[dict[str, str]]:
    return [{"url": p.url, "markdown": p.markdown, "title": p.title} for p in pages]
