"""
Unified page fetch engine interface for site crawling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Protocol
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.jina_reader_client import JinaReaderClient

EngineName = Literal["jina"]

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


class JinaFetchEngine(BaseFetchEngine):
    engine_name: EngineName = "jina"

    def __init__(self):
        self.client = JinaReaderClient()

    async def fetch(self, url: str) -> FetchedPage:
        page = await self.client.fetch(url)
        return FetchedPage(
            request_url=url,
            final_url=page.final_url,
            title=page.title,
            content=page.content,
            source_type="markdown",
            links=[FetchedLink(href=item.get("href", ""), text=item.get("text", "")) for item in page.links],
        )


def create_fetch_engine(engine_name: str = "jina") -> FetchEngine:
    normalized = (engine_name or "jina").strip().lower()
    if normalized == "jina":
        return JinaFetchEngine()
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
