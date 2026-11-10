from __future__ import annotations

import functools
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests
from tinycss2.bytes import decode_stylesheet_bytes

from utils.crawler_visibility_sheets import document_sheets, parse_document, parse_sheet, sheet_imports
from utils.url_safety import crawl_entry_url_error

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/css,*/*;q=0.1",
}
FETCH_WORKERS = 8
FETCH_ATTEMPTS = 3
RETRY_DELAY_S = 0.3
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
EMPTY_SHEET_STATUSES = frozenset({404, 410})
MAX_REDIRECTS = 5


def _get(url: str, timeout: float) -> Optional[requests.Response]:
    try:
        return requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=False)
    except requests.RequestException:
        return None  # 连接失败或超时：重试，仍失败时页面判定为不确定


def _get_with_retries(url: str, timeout: float) -> Optional[requests.Response]:
    response = None
    for attempt in range(FETCH_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_DELAY_S * attempt)
        response = _get(url, timeout)
        if response is not None and response.status_code not in RETRY_STATUSES:
            break
    return response


def _get_guarded(url: str, timeout: float) -> Optional[requests.Response]:
    """逐跳跟随重定向，每一跳都先经 utils.url_safety 检查；被拒绝或跳转过多时返回 None。"""
    for _ in range(MAX_REDIRECTS + 1):
        if crawl_entry_url_error(url):
            return None
        response = _get_with_retries(url, timeout)
        if response is None or not response.is_redirect:
            return response
        url = urljoin(url, response.headers["Location"])
    return None


@functools.lru_cache(maxsize=4096)
def _fetch_css(url: str, timeout: float) -> Optional[str]:
    """样式表文本；取不到（安全检查拒绝、连接失败、超时、其他非 2xx 状态）时返回 None。

    404/410 在 Chrome 中按空样式表处理，返回 ""。
    """
    response = _get_guarded(url, timeout)
    if response is not None and response.status_code in EMPTY_SHEET_STATUSES:
        return ""
    if response is None or not 200 <= response.status_code < 300:
        return None
    declared = "charset=" in response.headers.get("Content-Type", "").lower()
    text, _ = decode_stylesheet_bytes(response.content, protocol_encoding=response.encoding if declared else None)
    return text


def fetch_stylesheets(page_url: str, raw_html: Optional[str], *, timeout: float = 15.0) -> Dict[str, Optional[str]]:
    """抓取页面引用的全部外部样式表（含递归 @import），返回 {绝对地址: 文本}；取不到的为 None。"""
    pending: List[str] = []
    for ref in document_sheets(parse_document(raw_html or ""), page_url):
        if ref.url:
            pending.append(ref.url)
        else:
            pending.extend(url for url, _ in sheet_imports(parse_sheet(ref.css), ref.base_url))
    fetched: Dict[str, Optional[str]] = {}
    # 同一层引用的样式表并发抓取，再抓取它们 @import 的下一层
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        while pending:
            batch = [url for url in dict.fromkeys(pending) if url not in fetched]
            pending = []
            for url, text in zip(batch, pool.map(lambda target: _fetch_css(target, timeout), batch)):
                fetched[url] = text
                if text is not None:
                    pending.extend(target for target, _ in sheet_imports(parse_sheet(text), url))
    return fetched
