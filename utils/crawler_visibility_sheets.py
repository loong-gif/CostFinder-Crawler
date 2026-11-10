from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import tinycss2
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class SheetRef:
    """文档中按出现顺序引用的一份样式：外部样式表（url）或 <style> 块（css）。"""

    url: Optional[str]
    css: str
    media: str
    base_url: str


def parse_document(raw_html: str) -> BeautifulSoup:
    return BeautifulSoup(raw_html or "", "lxml")


def parse_sheet(css: str) -> list:
    return tinycss2.parse_stylesheet(css or "", skip_comments=True, skip_whitespace=True)


def _is_stylesheet_link(link) -> bool:
    rel = {value.lower() for value in link.get("rel", [])}
    if "alternate" in rel or link.has_attr("disabled"):
        return False
    if "stylesheet" in rel:
        return True
    # 异步加载写法：<link rel=preload as=style onload="this.rel='stylesheet'">，页面加载后即生效
    return "preload" in rel and link.get("as", "").lower() == "style" and "stylesheet" in link.get("onload", "")


def _link_media(link) -> str:
    # 异步加载写法：media="print" onload="this.media='all'"，页面加载后对屏幕生效
    return "" if "this.media" in link.get("onload", "") else link.get("media", "")


def document_sheets(soup: BeautifulSoup, page_url: str) -> List[SheetRef]:
    base = soup.find("base", href=True)
    base_url = urljoin(page_url, base["href"].strip()) if base else page_url
    refs: List[SheetRef] = []
    for node in soup.find_all(["link", "style"]):
        if node.find_parent(["noscript", "template"]):
            continue
        if node.name == "style":
            refs.append(SheetRef(None, node.get_text(), node.get("media", ""), base_url))
        elif _is_stylesheet_link(node) and node.get("href", "").strip():
            refs.append(SheetRef(urljoin(base_url, node["href"].strip()), "", _link_media(node), base_url))
    return refs


def _import_target(token) -> Optional[str]:
    if token.type in ("url", "string"):
        return token.value
    if token.type == "function" and token.lower_name == "url":
        strings = [t.value for t in token.arguments if t.type == "string"]
        return strings[0] if strings else None
    return None


def sheet_imports(rules: list, base_url: str) -> List[Tuple[str, list]]:
    """样式表开头的 @import：返回（绝对地址，媒体条件记号）。@import 只在其他规则之前有效。"""
    imports: List[Tuple[str, list]] = []
    for rule in rules:
        keyword = getattr(rule, "lower_at_keyword", None)
        if keyword == "charset" or (keyword == "layer" and rule.content is None):
            continue
        if keyword != "import":
            break
        prelude = [t for t in rule.prelude if t.type not in ("whitespace", "comment")]
        target = _import_target(prelude[0]) if prelude else None
        if target:
            imports.append((urljoin(base_url, target), prelude[1:]))
    return imports
