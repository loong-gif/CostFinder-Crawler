"""
Unified page fetch engine interface for site crawling.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Protocol
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from utils.firecrawl_client import get_firecrawl_client

EngineName = Literal["firecrawl"]

_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


@dataclass(frozen=True)
class FetchedLink:
    href: str
    text: str = ""


@dataclass(frozen=True)
class FetchedPage:
    request_url: str
    final_url: str
    title: str
    content: str
    source_type: Literal["html", "markdown"]
    links: List[FetchedLink] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class FetchEngine(Protocol):
    engine_name: EngineName

    async def start(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def fetch(self, url: str) -> FetchedPage:
        ...


class BaseFetchEngine:
    engine_name: EngineName

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _document_to_fetched_page(request_url: str, doc: Any) -> FetchedPage:
    metadata = getattr(doc, "metadata", None)
    final_url = (getattr(metadata, "url", None) or request_url).strip()
    title = (getattr(metadata, "title", None) or "").strip()
    content = (getattr(doc, "markdown", None) or "").strip()
    raw_links = getattr(doc, "links", None) or []
    links: List[FetchedLink] = []
    for item in raw_links:
        if isinstance(item, str) and item.strip():
            links.append(FetchedLink(href=item.strip(), text=""))
        elif isinstance(item, dict):
            href = str(item.get("href") or item.get("url") or "").strip()
            if href:
                links.append(FetchedLink(href=href, text=str(item.get("text") or "").strip()))
    return FetchedPage(
        request_url=request_url,
        final_url=final_url,
        title=title,
        content=content,
        source_type="markdown",
        links=links,
    )


class FirecrawlFetchEngine(BaseFetchEngine):
    engine_name: EngineName = "firecrawl"

    def __init__(self):
        self.client = get_firecrawl_client()

    async def fetch(self, url: str) -> FetchedPage:
        doc = await asyncio.to_thread(
            self.client.scrape,
            url,
            formats=["markdown", "links"],
        )
        return _document_to_fetched_page(url, doc)


def create_fetch_engine(engine_name: str = "firecrawl") -> FetchEngine:
    normalized = (engine_name or "firecrawl").strip().lower()
    if normalized == "firecrawl":
        return FirecrawlFetchEngine()
    raise ValueError(f"Unsupported fetch engine: {engine_name}")


def _extract_links_from_html(html: str, base_url: str) -> List[FetchedLink]:
    if not html.strip():
        return []
    soup = BeautifulSoup(html, "lxml")
    links: Dict[str, FetchedLink] = {}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor.get("href", "").strip())
        if not href.startswith(("http://", "https://")):
            continue
        links[href] = FetchedLink(href=href, text=anchor.get_text(strip=True))
    return list(links.values())


def _extract_links_from_markdown(markdown: str, base_url: str) -> List[FetchedLink]:
    if not markdown.strip():
        return []
    links: Dict[str, FetchedLink] = {}
    for text, href in _MARKDOWN_LINK_PATTERN.findall(markdown):
        absolute_href = urljoin(base_url, href.strip())
        if not absolute_href.startswith(("http://", "https://")):
            continue
        links[absolute_href] = FetchedLink(href=absolute_href, text=text.strip())
    return list(links.values())
