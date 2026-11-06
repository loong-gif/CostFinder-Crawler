"""促销卡片链接到的条款段落：卡片旁的「offer terms」类链接指向同站条款页的锚点，锚点所在段落是该卡片的条件与日期。

crawl 阶段抓取条款页，按锚点取出段落，并记下链接所在卡片的标题（链接之前最近的标题），存入 raw（linked_terms）；
extract-pages 在卡片通过校验后由代码把对应段落追加到卡片末尾（不经 LLM，结果确定）；
可见性清单同时渲染条款页核验这些段落中的内容。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from markdown_it import MarkdownIt

TERMS_LINK_TEXT_RE = re.compile(r"\b(?:offer terms|terms(?: and | & )conditions|terms|conditions|restrictions|"
                                r"disclaimers?|fine print|see details)\b", re.IGNORECASE)
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
NON_TEXT_TAGS = {"script", "style", "noscript", "template", "svg"}
MAX_SECTION_CHARS = 3000


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def terms_links(markdown: str, page_url: str) -> list[tuple[str, str, str]]:
    """页面上指向同站条款锚点的链接，按出现顺序去重：(条款页 URL，锚点，链接之前最近的标题)。"""
    found: list[tuple[str, str, str]] = []
    heading = ""
    tokens = MarkdownIt().parse(markdown)
    for index, token in enumerate(tokens):
        if token.type != "inline" or not token.children:
            continue
        if index > 0 and tokens[index - 1].type == "heading_open":
            heading = token.content.strip()
            continue
        href, text = None, []
        for child in token.children:
            if child.type == "link_open":
                href, text = str(child.attrGet("href") or ""), []
            elif child.type == "text" and href is not None:
                text.append(child.content)
            elif child.type == "link_close" and href is not None:
                absolute = urljoin(page_url, href)
                target, fragment = urldefrag(absolute)
                link = (target, fragment, heading)
                if (fragment and TERMS_LINK_TEXT_RE.search(" ".join(text))
                        and _host(target) == _host(page_url) and link not in found):
                    found.append(link)
                href = None
    return found


def _section_text(anchor: Tag) -> str:
    """锚点为标题时取标题及其后内容，直到同级或更高级的下一个标题；否则只取锚点元素本身的文字。"""
    if anchor.name not in HEADINGS:
        return " ".join(anchor.get_text(" ", strip=True).split())
    level = int(anchor.name[1])
    parts: list[str] = []
    for node in anchor.next_elements:
        if isinstance(node, Tag) and node.name in HEADINGS and node is not anchor and int(node.name[1]) <= level:
            break
        if isinstance(node, NavigableString) and node.parent is not None and node.parent.name not in NON_TEXT_TAGS:
            text = " ".join(str(node).split())
            if text:
                parts.append(text)
    return " ".join(" ".join(parts).split())[:MAX_SECTION_CHARS]


def terms_sections(html: str, fragments: Iterable[str]) -> dict[str, str]:
    """条款页 HTML 中各锚点的段落；同一 id 出现多次时取标题元素（导航按钮也可能带同名 id）。"""
    soup = BeautifulSoup(html, "html.parser")
    sections: dict[str, str] = {}
    for fragment in fragments:
        anchors = soup.find_all(id=fragment)
        if not anchors:
            continue
        anchor = next((a for a in anchors if a.name in HEADINGS), anchors[0])
        text = _section_text(anchor)
        if text:
            sections[fragment] = text
    return sections


def collect_linked_terms(markdown: str, page_url: str,
                         fetch_html: Callable[[str], str]) -> list[dict[str, Any]]:
    """抓取页面链接到的条款页（每页一次），返回 [{url, page, fragment, text, cards}]；cards 为链接该锚点的卡片标题。
    锚点在条款页上不存在的链接不返回。"""
    cards: dict[tuple[str, str], list[str]] = {}
    for target, fragment, heading in terms_links(markdown, page_url):
        titles = cards.setdefault((target, fragment), [])
        if heading and heading not in titles:
            titles.append(heading)
    linked: list[dict[str, Any]] = []
    for target in dict.fromkeys(t for t, _ in cards):
        fragments = [f for t, f in cards if t == target]
        sections = terms_sections(fetch_html(target), fragments)
        linked.extend({"url": f"{target}#{fragment}", "page": target, "fragment": fragment, "text": sections[fragment],
                       "cards": cards[(target, fragment)]} for fragment in fragments if fragment in sections)
    return linked


def card_key(title: str) -> str:
    """卡片标题比对键：只保留字母、数字与 %、$（脚注符号、markdown 转义与空白不影响对应）。"""
    return re.sub(r"[^0-9a-z%$]+", "", str(title or "").casefold())


def attach_terms(blocks: list[list[str]], linked_terms: Iterable[Mapping[str, Any]] | None) -> list[list[str]]:
    """卡片（首行为标题）末尾追加其链接到的条款段落；一张卡片对应多个锚点时按页面顺序全部追加。"""
    attached = []
    for block in blocks:
        title = card_key(block[0])
        texts = [item["text"] for item in linked_terms or []
                 if title and any(card_key(card) == title for card in item.get("cards") or [])]
        attached.append([*block, *(t for t in texts if t not in block)])
    return attached
