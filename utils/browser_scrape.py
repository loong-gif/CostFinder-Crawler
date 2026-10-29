"""经浏览器渲染页面并转为 markdown，返回与 Firecrawl scrape 相同结构的结果，供 crawl 写入 raw 存储。

lightpanda：远程 Lightpanda（CDP）；ego：本机 ego-browser（真实浏览器，带用户会话，可通过站点的反爬拦截）。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from markdownify import markdownify
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from utils.page_title import EGO_TASK_SPACE, LIGHTPANDA_CDP_URL, PAGE_TIMEOUT_MS, is_generic_title

NON_CONTENT_TAGS = ["script", "style", "noscript", "svg", "iframe", "template"]
EGO_PROCESS_TIMEOUT_S = 240
EGO_SCRIPT = """
const task = await taskSpace({space});
const page = task.page("p1");
await page.goto({url}, {{ timeout: {timeout} }});
await page.waitForTimeout(6000);
const info = await page.evaluate(() => ({{
  url: location.href,
  title: document.title,
  html: document.documentElement.outerHTML,
}}));
console.log("EGO_SCRAPE " + JSON.stringify(info));
"""


class BrowserScrapeFailed(Exception):
    pass


def html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(NON_CONTENT_TAGS):
        tag.decompose()
    body = soup.body or soup
    return markdownify(str(body), heading_style="ATX").strip()


def is_block_page(markdown: str) -> bool:
    """前三个标题中出现 HTTP 错误码或拦截页标题（站点以 200 返回的拦截页）。"""
    tokens = MarkdownIt().parse(markdown)
    headings = [tokens[i + 1].content for i, t in enumerate(tokens) if t.type == "heading_open"][:3]
    return any(is_generic_title(h) for h in headings)


def _result(url: str, final_url: str, title: str, html: str, status: int | None, engine: str) -> dict[str, Any]:
    markdown = html_to_markdown(html)
    if is_block_page(markdown):
        raise BrowserScrapeFailed("blocked_page")
    return {"markdown": markdown, "html": html,
            "metadata": {"title": title, "sourceURL": url, "url": final_url, "statusCode": status, "engine": engine}}


def lightpanda_scrape(url: str) -> dict[str, Any]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(LIGHTPANDA_CDP_URL)
        try:
            page = browser.new_context().new_page()
            try:
                response = page.goto(url, wait_until="load", timeout=PAGE_TIMEOUT_MS)
            except PlaywrightError as exc:
                # 导航失败（超时、网络错误）是该页面的抓取结果；只捕获 goto 本身的错误
                raise BrowserScrapeFailed(f"navigation_failed: {str(exc).splitlines()[0]}") from None
            status = response.status if response is not None else None
            if status is not None and status >= 400:
                raise BrowserScrapeFailed(f"http_{status}")
            html = page.content()
            title = page.title()
            final_url = page.url
        finally:
            browser.close()
    return _result(url, final_url, title, html, status, "lightpanda")


def ego_scrape(url: str) -> dict[str, Any]:
    script = EGO_SCRIPT.format(space=json.dumps(EGO_TASK_SPACE), url=json.dumps(url), timeout=PAGE_TIMEOUT_MS)
    try:
        done = subprocess.run(["ego-browser", "nodejs", "-e", script], capture_output=True, text=True, check=True,
                              timeout=EGO_PROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # 页面卡住（例如停在验证页）是该页面的抓取结果
        raise BrowserScrapeFailed("ego_timeout") from None
    # ego-browser 的 console 输出写到 stderr
    line = next(l for l in (done.stdout + done.stderr).splitlines() if l.startswith("EGO_SCRAPE "))
    info = json.loads(line[len("EGO_SCRAPE "):])
    return _result(url, info["url"], info["title"], info["html"], None, "ego")
