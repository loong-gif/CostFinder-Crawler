"""促销标题取自来源页面：<title> → og:title → 第一个 <h1>，原文不加工；通用标题跳过。

抓取层结果（Firecrawl data）里没有可用标题时，由 utils.ego_browser.fetch_page_title 经 ego-lite 渲染页面取值。
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from markdown_it import MarkdownIt

# 不能代表页面的标题：首页、空白页、错误页与拦截页；"Specials | 商家名" 是页面真实标题，不在此列
GENERIC_TITLE_RE = re.compile(
    r"^(home|home page|homepage|index|untitled|page not found|[45]\d\d|not found|access denied !?|forbidden|"
    r"just a moment\.*|attention required!?( \| cloudflare)?|sorry, you have been blocked|you have been blocked|"
    r"verify you are human|checking your browser.*)$",
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(r"\s*[|\-–—:•·]\s*")
PAGE_TIMEOUT_MS = 90_000


class TitleUnavailable(RuntimeError):
    pass


def is_generic_title(title: str, business_name: str = "") -> bool:
    text = " ".join(str(title or "").split())
    if not text or GENERIC_TITLE_RE.match(text):
        return True
    name = " ".join(str(business_name or "").split()).casefold()
    # 只剩站点名（或站点名加首页类词）的标题
    parts = [p for p in _SEPARATOR_RE.split(text) if p]
    return bool(name) and all(p.casefold() == name or GENERIC_TITLE_RE.match(p) for p in parts)


def choose_title(candidates: list[str], business_name: str = "") -> str:
    for candidate in candidates:
        text = " ".join(str(candidate or "").split())
        if text and not is_generic_title(text, business_name):
            return text
    raise TitleUnavailable("no_usable_title")


def is_block_page(markdown: str) -> bool:
    """前三个标题中出现 HTTP 错误码或拦截页标题（站点以 200 返回的拦截页）。"""
    tokens = MarkdownIt().parse(markdown)
    headings = [tokens[i + 1].content for i, t in enumerate(tokens) if t.type == "heading_open"]
    # 没有文字的空标题是页面的装饰结构，不算拦截页标题
    return any(is_generic_title(h) for h in [h for h in headings if h.strip()][:3])


def title_candidates_from_scrape(data: Mapping[str, Any]) -> list[str]:
    """data 为 Firecrawl /v1/scrape 返回的 data 字段。"""
    meta = data.get("metadata") or {}

    def first(value: Any) -> str:
        return str((value[0] if isinstance(value, list) and value else value) or "")

    return [first(meta.get("title")), first(meta.get("ogTitle") or meta.get("og:title")),
            first_h1(str(data.get("markdown") or ""))]




def first_h1(markdown: str) -> str:
    tokens = MarkdownIt().parse(markdown)
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h1":
            return tokens[index + 1].content
    return ""


