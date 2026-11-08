"""经浏览器渲染页面并转为 markdown，返回与 Firecrawl scrape 相同结构的结果，供 crawl 写入 raw 存储。

scrapling：HTTP 抓取与隐身浏览器；ego-lite 渲染见 utils.ego_browser。
"""
from __future__ import annotations

from typing import Any

from curl_cffi.requests.exceptions import RequestException
from scrapling.fetchers import Fetcher, StealthyFetcher

from utils.ego_browser import BrowserScrapeFailed, ego_scrape, firecrawl_shaped_result as _result, html_to_markdown
from utils.page_title import PAGE_TIMEOUT_MS

__all__ = ["BrowserScrapeFailed", "ego_scrape", "html_to_markdown", "scrapling_browser_scrape", "scrapling_scrape"]


def scrapling_scrape(url: str) -> dict[str, Any]:
    """Scrapling 的 HTTP 抓取（Chrome TLS 指纹）；HTTP 状态码不是 200 是该页面的抓取结果。"""
    # 本机经 127.0.0.1 代理出网，默认的 safe 重定向会把代理地址当成内网地址拒绝，所以显式允许重定向
    try:
        page = Fetcher.get(url, impersonate="chrome", timeout=30, retries=1, follow_redirects=True)
    except RequestException as exc:
        # 连接被站点断开、超时、证书错误是该页面的抓取结果
        raise BrowserScrapeFailed(f"scrapling_request_failed: {str(exc)[:200]}") from None
    if page.status != 200:
        raise BrowserScrapeFailed(f"scrapling_status_{page.status}")
    return _result(url, str(page.url), page.css("title::text").get() or "", page.html_content, page.status, "scrapling")


def scrapling_browser_scrape(url: str) -> dict[str, Any]:
    """Scrapling 的隐身浏览器渲染，等待网络空闲后取渲染后的 HTML。"""
    page = StealthyFetcher.fetch(url, headless=True, network_idle=True, block_ads=True, timeout=60000)
    if page.status != 200:
        raise BrowserScrapeFailed(f"scrapling_status_{page.status}")
    return _result(url, str(page.url), page.css("title::text").get() or "", page.html_content, page.status,
                   "scrapling-browser")
