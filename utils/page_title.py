"""促销标题取自来源页面：<title> → og:title → 第一个 <h1>，原文不加工；通用标题跳过。

现抓页面先经 Lightpanda（CDP）渲染；被拦截或超时的页面交给 ego-lite 渲染核验。抓取层结果（Firecrawl data）同口径取值。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any, Mapping

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

# 不能代表页面的标题：首页、空白页、错误页与拦截页；"Specials | 商家名" 是页面真实标题，不在此列
GENERIC_TITLE_RE = re.compile(
    r"^(home|home page|homepage|index|untitled|page not found|[45]\d\d|not found|access denied !?|forbidden|"
    r"just a moment\.*|attention required!?( \| cloudflare)?|sorry, you have been blocked|you have been blocked|"
    r"verify you are human|checking your browser.*)$",
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(r"\s*[|\-–—:•·]\s*")
LIGHTPANDA_CDP_URL = os.getenv("LIGHTPANDA_CDP_URL", "ws://192.168.1.68:9222")
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


def title_candidates_from_html(document_title: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", attrs={"property": "og:title"})
    h1 = soup.find("h1")
    return [document_title, str(og.get("content") or "") if og else "", h1.get_text(" ") if h1 else ""]


def first_h1(markdown: str) -> str:
    tokens = MarkdownIt().parse(markdown)
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.tag == "h1":
            return tokens[index + 1].content
    return ""


def fetch_page_title(url: str, business_name: str = "") -> str:
    """先经 Lightpanda 取标题；Lightpanda 被拦截、超时或只拿到拦截页时，交给 ego-lite（真实浏览器）渲染核验。
    页面 404 不再交给 ego-lite。"""
    try:
        return lightpanda_page_title(url, business_name)
    except TitleUnavailable as exc:
        if str(exc) == "http_404":
            raise
    return ego_page_title(url, business_name)


EGO_TASK_SPACE = "costfinder page titles"
EGO_SCRIPT = """
const task = await taskSpace({space});
const page = task.page("p1");
await page.goto({url}, {{ timeout: {timeout} }});
await page.waitForTimeout(6000);
const info = await page.evaluate(() => ({{
  title: document.title,
  og: document.querySelector('meta[property="og:title"]')?.content || "",
  h1: document.querySelector("h1")?.innerText || "",
}}));
console.log("EGO_TITLE " + JSON.stringify(info));
"""


def ego_page_title(url: str, business_name: str = "") -> str:
    script = EGO_SCRIPT.format(space=json.dumps(EGO_TASK_SPACE), url=json.dumps(url), timeout=PAGE_TIMEOUT_MS)
    done = subprocess.run(["ego-browser", "nodejs", "-e", script], capture_output=True, text=True, check=True)
    # ego-browser 的 console 输出写到 stderr
    line = next(l for l in (done.stdout + done.stderr).splitlines() if l.startswith("EGO_TITLE "))
    info = json.loads(line[len("EGO_TITLE "):])
    return choose_title([info["title"], info["og"], info["h1"]], business_name)


def lightpanda_page_title(url: str, business_name: str = "") -> str:
    """经 Lightpanda 渲染页面后取标题；HTTP 错误、拦截页与超时是该页面的结果，记为 TitleUnavailable。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(LIGHTPANDA_CDP_URL)
        try:
            page = browser.new_context().new_page()
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            except PlaywrightError as exc:
                # 导航失败（超时、网络错误）是该页面的结果；只捕获 goto 本身的错误
                raise TitleUnavailable(f"navigation_failed: {str(exc).splitlines()[0]}") from None
            if response is not None and response.status >= 400:
                raise TitleUnavailable(f"http_{response.status}")
            candidates = title_candidates_from_html(page.title(), page.content())
        finally:
            browser.close()
    return choose_title(candidates, business_name)
