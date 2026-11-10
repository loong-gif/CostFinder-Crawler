"""监控抓取阶段每个页面的可见性证据：爬虫内容经 CSS 层叠判定完全可见时直接作为证据，其余页面由 ego-lite 渲染。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from utils import monitor_rendered_browser
from utils.crawler_visibility import assess_page
from utils.crawler_visibility_fetch import fetch_stylesheets
from utils.monitor_price_store import atomic_json
from utils.monitor_visibility_evidence import CRAWLER_WIDTH, CrawlerVisibility, visibility_source

# 商品页与商品集合页的规格选项只能在浏览器里逐个选择截图
PRODUCT_PATH_PARTS = ("/products/", "/collections/")


def _crawl_reasons(url: str, page, data: dict) -> tuple[str, ...]:
    """爬虫结果本身不能作为证据的原因：抓取失败、非 2xx（含 404/410 下架与 ego-lite 兜底抓取）、商品规格页。"""
    if page.status == "error":
        return ("scrape_failed",)
    status = (data.get("metadata") or {}).get("statusCode")
    if not (isinstance(status, int) and 200 <= status < 300):
        return ("crawl_status_not_ok",)
    if any(part in urlsplit(url).path for part in PRODUCT_PATH_PARTS):
        return ("product_variants_require_browser",)
    return ()


def render_capture(url: str, state_dir: Path) -> dict:
    """ego-lite 渲染一次；渲染异常记录在渲染目录，页面按不可访问处理。"""
    directory = state_dir / "rendered" / uuid4().hex
    try:
        browser = monitor_rendered_browser.render_page(url, directory)
        return {"browser": browser, "browser_path": str(directory / "browser.json")}
    except Exception as exc:
        failure = {"error": type(exc).__name__, "browser_path": str(directory / "browser.json")}
        atomic_json(directory / "capture_failure.json", failure)
        return failure


def _visibility(url: str, page, data: dict, raw_html: str, state_dir: Path) -> dict:
    reasons = _crawl_reasons(url, page, data)
    if not reasons:
        metadata = data["metadata"]
        final_url = metadata.get("url") or url
        assessment = assess_page(page.markdown, raw_html, page.facts, page_url=final_url,
                                 stylesheets=fetch_stylesheets(final_url, raw_html), width=CRAWLER_WIDTH)
        if not assessment.needs_browser:
            evidence = CrawlerVisibility(url=url, final_url=final_url, http_status=metadata["statusCode"],
                                         title=str(metadata.get("title") or ""), text=assessment.visible_text,
                                         context_digest=assessment.visible_digest, captured_at=page.captured_at)
            return {"browser": evidence.as_json(), "crawler_reasons": []}
        reasons = assessment.reasons
    return {**render_capture(url, state_dir), "crawler_reasons": list(reasons)}


def page_visibility(url: str, page, state_dir: Path, cache: dict) -> dict:
    """返回 {"browser": 证据, "crawler_reasons": [...]}，渲染证据另有 browser_path。

    同一轮运行里同一地址只判定一次（cache 跨监控组共用）。
    """
    data = (page.response or {}).get("data") or {}
    raw_html = data.pop("rawHtml", "")  # 判定之后不再需要，证据文件不保存整页 HTML
    if url not in cache:
        cache[url] = _visibility(url, page, data, raw_html, state_dir)
    return cache[url]


def visibility_row(url: str, result: dict) -> dict:
    """运行报告里每个页面的可见性证据来源与状态。"""
    browser = result.get("browser") or {}
    return {"url": url, "visibility_source": visibility_source(browser), "accessible": browser.get("accessible") is True,
            "complete": browser.get("complete") is True, "http_status": browser.get("http_status"),
            "image_review_required": browser.get("image_review_required"),
            "crawler_reasons": result["crawler_reasons"], "browser_path": result.get("browser_path"),
            "error": result.get("error")}
