"""Shared Firecrawl SDK client factory."""
from __future__ import annotations

import os
from typing import Any, Callable, Dict

import requests
from dotenv import load_dotenv
from pathlib import Path

from utils.page_title import is_block_page

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_firecrawl_env(project_root: Path | None = None) -> None:
    root = project_root or PROJECT_ROOT
    load_dotenv(root / ".env")


def get_firecrawl_client(*, project_root: Path | None = None):
    """Initialize Firecrawl client with API key and optional self-hosted URL from env."""
    return _build_firecrawl_client(
        api_key_env="FIRECRAWL_API_KEY",
        api_url_env="FIRECRAWL_API_URL",
        project_root=project_root,
    )


def get_firecrawl_cloud_client(*, project_root: Path | None = None):
    """Initialize Firecrawl Cloud without inheriting a self-hosted API URL."""
    load_firecrawl_env(project_root)
    api_key = (
        os.getenv("FIRECRAWL_CLOUD_API_KEY")
        or os.getenv("FIRECRAWL_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("Missing FIRECRAWL_CLOUD_API_KEY or FIRECRAWL_API_KEY")
    return _build_firecrawl_client(
        api_key_env="FIRECRAWL_CLOUD_API_KEY",
        api_url_env="FIRECRAWL_CLOUD_API_URL",
        project_root=project_root,
        default_api_url="https://api.firecrawl.dev",
        fallback_key_env="FIRECRAWL_API_KEY",
        api_key=api_key,
    )


def get_firecrawl_search_client(*, project_root: Path | None = None):
    """Firecrawl Cloud client for search (does not inherit self-hosted FIRECRAWL_API_URL)."""
    load_firecrawl_env(project_root)
    search_key = (os.getenv("FIRECRAWL_SEARCH_API_KEY") or "").strip()
    crawl_url = (os.getenv("FIRECRAWL_API_URL") or "").strip().lower()
    if not search_key:
        if not crawl_url or "api.firecrawl.dev" in crawl_url:
            search_key = (os.getenv("FIRECRAWL_API_KEY") or "").strip()
        else:
            raise RuntimeError(
                "Firecrawl Search requires FIRECRAWL_SEARCH_API_KEY (cloud API key). "
                "Self-hosted FIRECRAWL_API_KEY cannot call search."
            )
    return _build_firecrawl_client(
        api_key_env="FIRECRAWL_SEARCH_API_KEY",
        api_url_env="FIRECRAWL_SEARCH_API_URL",
        project_root=project_root,
        default_api_url="https://api.firecrawl.dev",
        fallback_key_env="",
        api_key=search_key,
    )


def _build_firecrawl_client(
    *,
    api_key_env: str,
    api_url_env: str,
    project_root: Path | None = None,
    default_api_url: str = "",
    fallback_key_env: str = "",
    api_key: str = "",
):
    from firecrawl import Firecrawl

    load_firecrawl_env(project_root)

    if not api_key:
        api_key = (os.getenv(api_key_env) or "").strip()
    if not api_key and fallback_key_env:
        api_key = (os.getenv(fallback_key_env) or "").strip()
    if not api_key:
        raise RuntimeError(
            f"Missing {api_key_env}. Add it to .env or export it.\n"
            "Get your key at: https://firecrawl.dev/app/api-keys"
        )
    timeout = float(os.getenv("FIRECRAWL_HTTP_TIMEOUT_SECS", "120"))
    client_kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout,
        "max_retries": 2,
    }
    api_url = (os.getenv(api_url_env) or default_api_url or "").strip()
    if api_url:
        client_kwargs["api_url"] = api_url.rstrip("/")
    return Firecrawl(**client_kwargs)


class SelfHostedFirecrawl:
    """爬虫层统一使用的自托管 Firecrawl（v1 API）。"""

    def __init__(self, api_url: str, api_key: str = "", timeout: float = 120, *,
                 browser: Callable[[str], dict[str, Any]]) -> None:
        """browser：fastMode 抓不到正文时渲染页面的真实浏览器（ego-lite），返回与 Firecrawl data 同构的结果。"""
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.browser = browser
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers["Content-Type"] = "application/json"
        if api_key:
            self.session.headers["Authorization"] = f"Bearer {api_key}"

    def scrape(self, url: str, formats: tuple[str, ...] = ("markdown",), *,
               only_main_content: bool = True) -> dict[str, Any]:
        """先 fastMode（只走 HTTP fetch 引擎，几秒完成）；拿不到正文时由 self.browser（ego-lite）渲染。

        Firecrawl 的浏览器引擎（容器内 Chromium）在 monitor 主机的 WSL1 上会卡住并把其他页面的内容返回给当前请求，
        所以不再使用。fastMode 下站点（例如 Cloudflare）返回的拦截页仍是 HTTP 200、success=true，只能从
        metadata.statusCode 和正文标题判定。页面已不存在（404/410）是该页面的结果，抛出 PageGone。
        """
        fast = self.session.post(f"{self.api_url}/v1/scrape", timeout=self.timeout, json={
            "url": url, "formats": list(formats), "onlyMainContent": only_main_content, "fastMode": True})
        data = fast.json().get("data") if fast.ok else None
        if data is not None and page_gone(data):
            raise PageGone(f"page_gone_{data['metadata']['statusCode']}: {url}", response=fast)
        if data is not None and fetched_content(data):
            return data
        return self.browser(url)


GONE_STATUSES = frozenset({404, 410})


class PageGone(requests.HTTPError):
    """站点对该地址返回 404 或 410。"""


def page_gone(data: dict[str, Any]) -> bool:
    status = (data.get("metadata") or {}).get("statusCode")
    return bool(status) and int(status) in GONE_STATUSES


def fetched_content(data: dict[str, Any]) -> bool:
    """fastMode 拿到了页面正文：状态码不是错误，正文非空，且不是拦截页。"""
    status = (data.get("metadata") or {}).get("statusCode")
    markdown = str(data.get("markdown") or "")
    return not (status and int(status) >= 400) and bool(markdown.strip()) and not is_block_page(markdown)


def self_hosted_firecrawl(*, browser: Callable[[str], dict[str, Any]],
                          project_root: Path | None = None) -> SelfHostedFirecrawl:
    load_firecrawl_env(project_root)
    return SelfHostedFirecrawl(os.environ["FIRECRAWL_SELF_HOSTED_API_URL"].strip("'\""),
                               os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY", ""), browser=browser)


def scrape_page_markdown(fc: Any, url: str) -> tuple[str, dict[str, Any]]:
    """Scrape one URL with project defaults (onlyMainContent + blockAds + denoise)."""
    from utils.firecrawl_scrape_raw_db import DEFAULT_SCRAPE_FORMATS
    from utils.scrape_markdown import prepare_scrape_markdown

    doc = fc.scrape(url, formats=list(DEFAULT_SCRAPE_FORMATS), only_main_content=True, block_ads=True)
    body = doc.model_dump() if hasattr(doc, "model_dump") else dict(doc or {})
    raw_md = str((body.get("markdown") if isinstance(body, dict) else "") or "")
    if not raw_md and isinstance(body, dict):
        data = body.get("data") or {}
        if isinstance(data, dict):
            raw_md = str(data.get("markdown") or "")
    md = prepare_scrape_markdown(raw_md)
    return md, body
