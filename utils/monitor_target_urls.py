"""Pick Firecrawl monitor scrape URLs from promo_website_staging subpage_url rows."""
from __future__ import annotations

from typing import Iterable, List
from urllib.parse import parse_qsl, urlparse, urlunparse

from crawler.promo_site_crawler import (
    STRONG_SIGNAL_KEYWORDS,
    TRACKING_QUERY_KEYS,
    normalize_domain,
    score_candidate_link,
    should_exclude_candidate,
)

_MIN_STRONG_SCORE = 4


def normalize_monitor_url(url: str) -> str:
    """Canonical URL for monitor targets: https, no tracking query, stable host."""
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return (url or "").strip()

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path

    return urlunparse(("https", host, path, "", urlencode_pairs(query_pairs), ""))


def urlencode_pairs(pairs: List[tuple]) -> str:
    if not pairs:
        return ""
    from urllib.parse import urlencode

    return urlencode(pairs)


def _dedupe_urls(urls: Iterable[str]) -> List[str]:
    seen_paths: set[str] = set()
    out: List[str] = []
    for raw in urls:
        normalized = normalize_monitor_url(raw)
        if not normalized:
            continue
        parsed = urlparse(normalized)
        key = f"{parsed.netloc.lower()}{parsed.path.rstrip('/') or '/'}"
        if key in seen_paths:
            continue
        seen_paths.add(key)
        out.append(normalized)
    return out


def pick_monitor_urls(
    subpage_urls: Iterable[str],
    *,
    domain_name: str,
    max_urls: int = 2,
) -> List[str]:
    """Return up to max_urls promo/pricing pages; fallback to homepage."""
    max_urls = max(1, max_urls)
    domain = normalize_domain(domain_name) or domain_name.strip().lower()

    scored: List[tuple[int, bool, str]] = []
    for raw in subpage_urls:
        url = (raw or "").strip()
        if not url or should_exclude_candidate(url):
            continue
        score = score_candidate_link(url)
        haystack = url.lower()
        has_strong = any(keyword in haystack for keyword in STRONG_SIGNAL_KEYWORDS)
        scored.append((score, has_strong, normalize_monitor_url(url)))

    scored = [(s, hs, u) for s, hs, u in scored if u]
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

    strong = [u for s, hs, u in scored if hs and s >= _MIN_STRONG_SCORE]
    if strong:
        return _dedupe_urls(strong)[:max_urls]

    if scored and scored[0][0] >= 3:
        return _dedupe_urls([scored[0][2]])[:max_urls]

    if scored:
        return _dedupe_urls([scored[0][2]])[:max_urls]

    return [f"https://{domain}"]
