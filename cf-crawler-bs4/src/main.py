from __future__ import annotations

import asyncio
import re
from collections import deque
from urllib.parse import urljoin

from apify import Actor, Event
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

from src.utils import (
    extract_ocr_text_from_page_screenshot,
    extract_page_name,
    fetch_sitemap_urls,
    filter_urls_by_inclusion,
    is_root_url,
    is_same_domain,
    normalize_domain,
    normalize_url,
    prepare_ocr_page_export,
    prepare_page_export,
    score_candidate_url,
    should_skip_url,
)

DISCOVERY_SCORE_THRESHOLD = 3
MIN_HIGH_CONFIDENCE_URLS = 3

DISCOVERY_EXCLUDE_PATTERNS = [
    re.compile(r".*\?.*", re.IGNORECASE),
    re.compile(r".*login.*", re.IGNORECASE),
    re.compile(r".*cart.*", re.IGNORECASE),
    re.compile(r".*checkout.*", re.IGNORECASE),
    re.compile(r".*privacy.*", re.IGNORECASE),
    re.compile(r".*terms.*", re.IGNORECASE),
    re.compile(r".*/about(?:-us)?(?:/|$).*", re.IGNORECASE),
    re.compile(r".*/contact(?:-us)?(?:/|$).*", re.IGNORECASE),
    re.compile(r".*/polic(?:y|ies)(?:/|$).*", re.IGNORECASE),
    re.compile(r".*/blogs?/.*", re.IGNORECASE),
    re.compile(r".*/learn(?:/|$).*", re.IGNORECASE),
    re.compile(r".*/news(?:/|$).*", re.IGNORECASE),
    re.compile(r".*/article(?:s)?(?:/|$).*", re.IGNORECASE),
    re.compile(r".*before-and-after.*", re.IGNORECASE),
    re.compile(r".*\.pdf$", re.IGNORECASE),
    re.compile(r".*\.jpg$", re.IGNORECASE),
    re.compile(r".*\.jpeg$", re.IGNORECASE),
    re.compile(r".*\.png$", re.IGNORECASE),
    re.compile(r".*\.gif$", re.IGNORECASE),
    re.compile(r".*\.webp$", re.IGNORECASE),
    re.compile(r".*test-.*", re.IGNORECASE),
]


def is_near_duplicate_page(current_keys: set[str], seen_pages: list[dict[str, object]]) -> bool:
    if len(current_keys) < 3:
        return False
    for seen_page in seen_pages:
        seen_keys = seen_page.get("segment_keys", set())
        if not isinstance(seen_keys, set) or len(seen_keys) < 3:
            continue
        overlap = len(current_keys & seen_keys)
        smaller_size = min(len(current_keys), len(seen_keys))
        if smaller_size and overlap / smaller_size >= 0.8:
            return True
    return False


def extract_html_from_crawl_result(crawl_result: object) -> str:
    for attr in ("html", "cleaned_html"):
        value = getattr(crawl_result, attr, "")
        if isinstance(value, str) and value.strip():
            return value

    markdown_obj = getattr(crawl_result, "markdown", None)
    if markdown_obj is not None:
        for attr in ("raw_markdown", "fit_markdown"):
            value = getattr(markdown_obj, attr, "")
            if isinstance(value, str) and value.strip():
                return value

    extracted_content = getattr(crawl_result, "extracted_content", "")
    if isinstance(extracted_content, str) and extracted_content.strip():
        return extracted_content

    return ""


def should_exclude_discovery_url(url: str) -> bool:
    return any(pattern.search(url) for pattern in DISCOVERY_EXCLUDE_PATTERNS)


def extract_discovery_links(base_url: str, html: str, target_domain: str) -> list[str]:
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    discovered: set[str] = set()

    for link in soup.find_all("a", href=True):
        href = str(link.get("href", "")).strip()
        if not href:
            continue
        candidate = normalize_url(urljoin(base_url, href))
        if not candidate:
            continue
        if not is_same_domain(candidate, target_domain):
            continue
        if should_skip_url(candidate):
            continue
        if should_exclude_discovery_url(candidate):
            continue
        discovered.add(candidate)

    return sorted(discovered, key=lambda item: (-score_candidate_url(item), item))


async def main() -> None:
    async with Actor:
        async def on_aborting() -> None:
            await asyncio.sleep(1)
            await Actor.exit()

        Actor.on(Event.ABORTING, on_aborting)

        actor_input = await Actor.get_input() or {}
        raw_website_url = actor_input.get("website_url")
        start_urls_input = actor_input.get("start_urls", [])
        max_pages = int(actor_input.get("maxCrawlPages", 50) or 50)
        global_needs_ocr = bool(actor_input.get("needs_ocr", False))

        target_website = normalize_url(raw_website_url or "")
        if not target_website and start_urls_input:
            first = start_urls_input[0]
            first_url = first.get("url") if isinstance(first, dict) else first
            target_website = normalize_url(first_url or "")

        input_domain = normalize_domain(target_website or raw_website_url or "")
        final_start_urls_set: set[str] = set()
        url_needs_ocr_map: dict[str, bool] = {}
        sitemap_urls: list[str] = []
        seen_content_signatures: set[str] = set()
        seen_export_pages: list[dict[str, object]] = []

        def record_url_ocr_flag(url: str, needs_ocr: bool) -> None:
            if not url:
                return
            url_needs_ocr_map[url] = url_needs_ocr_map.get(url, False) or needs_ocr

        if target_website:
            final_start_urls_set.add(target_website)
            record_url_ocr_flag(target_website, global_needs_ocr)
            if is_root_url(target_website):
                Actor.log.info(f"检测到输入为主域名，正在检索 Sitemap: {target_website}")
                sitemap_urls = await fetch_sitemap_urls(target_website)
            else:
                Actor.log.info(f"检测到输入为具体子页面，直接进入精准爬取模式: {target_website}")

        for value in start_urls_input:
            needs_ocr_for_item = global_needs_ocr
            raw_url = value.get("url") if isinstance(value, dict) else value
            if isinstance(value, dict):
                user_data = value.get("userData") if isinstance(value.get("userData"), dict) else {}
                needs_ocr_for_item = bool(value.get("needs_ocr", user_data.get("needs_ocr", global_needs_ocr)))
            normalized = normalize_url(raw_url or "")
            if normalized and not normalized.endswith(".xml"):
                final_start_urls_set.add(normalized)
                record_url_ocr_flag(normalized, needs_ocr_for_item)

        high_confidence_urls = [
            url for url in sitemap_urls if normalize_domain(url) == input_domain and score_candidate_url(url) >= 4
        ]
        for url in high_confidence_urls[:10]:
            final_start_urls_set.add(url)
            record_url_ocr_flag(url, global_needs_ocr)

        final_start_urls = filter_urls_by_inclusion(list(final_start_urls_set))
        if not final_start_urls:
            Actor.log.error("未找到可用的起始 URL，请提供 website_url 或 start_urls。")
            return

        has_ocr_targets = global_needs_ocr or any(url_needs_ocr_map.values())
        ocr_semaphore = asyncio.Semaphore(1) if has_ocr_targets else None
        enable_discovery = bool(
            target_website and is_root_url(target_website) and len(high_confidence_urls) < MIN_HIGH_CONFIDENCE_URLS
        )
        if enable_discovery:
            Actor.log.info("高置信候选页不足，开启 same-domain 自动发现模式")

        queue: deque[tuple[str, bool]] = deque(
            (url, url_needs_ocr_map.get(url, global_needs_ocr)) for url in final_start_urls
        )
        seen_urls: set[str] = set()

        browser_config = BrowserConfig(headless=True, verbose=False)
        crawler = AsyncWebCrawler(config=browser_config)
        await crawler.start()

        try:
            while queue and len(seen_urls) < max_pages:
                current_url, needs_ocr_for_request = queue.popleft()
                url = normalize_url(current_url)
                if not url or should_skip_url(url) or url in seen_urls:
                    continue
                seen_urls.add(url)

                Actor.log.info(f"正在处理: {url}")

                try:
                    run_config = CrawlerRunConfig(
                        cache_mode=CacheMode.BYPASS,
                        screenshot=bool(needs_ocr_for_request),
                    )
                    crawl_result = await crawler.arun(url=url, config=run_config)
                except Exception as exc:
                    Actor.log.warning(f"crawl4ai 抓取失败: {url}, error={exc}")
                    continue

                if not getattr(crawl_result, "success", False):
                    error_message = getattr(crawl_result, "error_message", "unknown crawl error")
                    Actor.log.warning(f"抓取失败: {url}, error={error_message}")
                    continue

                html = extract_html_from_crawl_result(crawl_result)
                soup = BeautifulSoup(html, "html.parser") if html else None

                if needs_ocr_for_request:
                    if ocr_semaphore is not None:
                        async with ocr_semaphore:
                            ocr_result = await extract_ocr_text_from_page_screenshot(url)
                    else:
                        ocr_result = await extract_ocr_text_from_page_screenshot(url)

                    ocr_error = ocr_result.get("error", "")
                    if ocr_error:
                        Actor.log.warning(f"OCR 预处理失败，回退 HTML 流程: {url}, error={ocr_error}")
                        if soup is None:
                            continue
                        page_payload = prepare_page_export(soup, url)
                    else:
                        page_payload = prepare_ocr_page_export(ocr_result.get("text", ""), url)
                        if not page_payload["page_content"]:
                            Actor.log.warning(f"OCR 文本为空，回退 HTML 流程: {url}")
                            if soup is None:
                                continue
                            page_payload = prepare_page_export(soup, url)
                else:
                    if soup is None:
                        continue
                    page_payload = prepare_page_export(soup, url)

                if not page_payload["should_export"]:
                    pass
                else:
                    content_signature = page_payload.get("content_signature", "")
                    if content_signature and content_signature in seen_content_signatures:
                        Actor.log.info(f"跳过重复内容页: {url}")
                    else:
                        current_segment_keys = set(page_payload.get("segment_keys", []))
                        if is_near_duplicate_page(current_segment_keys, seen_export_pages):
                            Actor.log.info(f"跳过高重合内容页: {url}")
                        else:
                            if content_signature:
                                seen_content_signatures.add(content_signature)
                            if current_segment_keys:
                                seen_export_pages.append({"url": url, "segment_keys": current_segment_keys})

                            await Actor.push_data(
                                {
                                    "subpage_url": url,
                                    "page_content": page_payload["page_content"],
                                    "domain": normalize_domain(url),
                                    "name": extract_page_name(soup, fallback=input_domain) if soup is not None else input_domain,
                                }
                            )

                if enable_discovery and soup is not None and len(seen_urls) < max_pages:
                    should_discover = is_root_url(url) or score_candidate_url(url) >= DISCOVERY_SCORE_THRESHOLD
                    if should_discover:
                        discovered = extract_discovery_links(url, html, input_domain)
                        for discovered_url in discovered:
                            if discovered_url in seen_urls:
                                continue
                            if len(seen_urls) + len(queue) >= max_pages * 4:
                                break
                            queue.append((discovered_url, needs_ocr_for_request))
        finally:
            await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())
