"""Firecrawl Search orchestration for clinic_services price discovery."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Sequence
from urllib.parse import urlparse

from utils.clinic_services_botox import website_to_crawl_url

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
