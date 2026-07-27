"""Bounded public-page fallback when the primary crawler is unavailable."""
from __future__ import annotations

from typing import Any

import requests
from bs4 import BeautifulSoup

from utils.url_safety import assert_safe_crawl_entry_url

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CostfinderServiceBot/1.0; +https://costfinder.example)",
    "Accept": "text/html,application/xhtml+xml",
}


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def scrape_public_page_text(url: str, *, timeout_seconds: float = 12) -> tuple[str, dict[str, Any]]:
    """Fetch a publicly accessible HTML document without bypassing access controls."""
    safe_url = assert_safe_crawl_entry_url(url)
    response = requests.get(safe_url, headers=DEFAULT_HEADERS, timeout=timeout_seconds)
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").casefold()
    if "html" not in content_type:
        raise ValueError(f"unsupported_content_type:{content_type or 'missing'}")
    text = html_to_text(response.text)
    if not text:
        raise ValueError("empty_public_page")
    return text, {
        "engine": "direct_public_http",
        "source_url": response.url,
        "status_code": response.status_code,
        "markdown": text,
    }
