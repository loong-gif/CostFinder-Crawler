"""爬虫层：用自托管 Firecrawl 抓取已决定加入的页面，原始结果写入本地 raw 文件存储。"""
import argparse
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from dotenv import load_dotenv

from utils.firecrawl_client import self_hosted_firecrawl
from utils.firecrawl_scrape_raw_db import save_scrape_response, scrape_request_fingerprint
from utils.browser_scrape import BrowserScrapeFailed, ego_scrape, scrapling_browser_scrape, scrapling_scrape
from utils.linked_terms import collect_linked_terms
from utils.raw_file_store import RawFileStore
from utils.scrape_markdown import banner_offer_line, prepare_scrape_markdown, site_banner_blocks
from utils.source_url import is_hub_url
from utils.paddle_ocr_markdown import build_markdown_ocr, filter_promo_image_urls, image_urls_from_markdown

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, default=PROJECT_ROOT / "state/discovery/monitor_decisions_final.json")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--engine", choices=["firecrawl", "ego", "scrapling", "scrapling-browser"],
                        default="firecrawl",
                        help="firecrawl 为 fastMode 抓取、失败时改由 ego-lite 渲染；ego 直接由本机 ego-browser 渲染；"
                             "scrapling 为 HTTP 抓取，scrapling-browser 为隐身浏览器渲染")
    parser.add_argument("--url", action="append", help="只抓取决定文件中的这些 URL，可重复")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("COSTFINDER_RAW_DIR"):
        raise RuntimeError("set COSTFINDER_RAW_DIR so the batch stays isolated")
    # Firecrawl fastMode 拿不到正文时由 ego-lite（真实浏览器）渲染
    firecrawl = self_hosted_firecrawl(browser=ego_scrape) if args.engine == "firecrawl" else None
    store = RawFileStore()

    decisions = json.loads(args.decisions.read_text())["decisions"]
    decisions_by_url = {record["url"]: record for record in decisions if record["decision"] == "add"}
    urls = sorted({r["url"] for r in decisions if r["decision"] == "add"})
    if args.url:
        missing = set(args.url) - set(urls)
        if missing:
            raise ValueError(f"URL not in decisions as add: {sorted(missing)}")
        urls = sorted(args.url)
    urls = urls[: args.limit]

    def fetch(url: str) -> dict:
        if args.engine == "ego":
            return ego_scrape(url)
        if args.engine == "scrapling":
            return scrapling_scrape(url)
        if args.engine == "scrapling-browser":
            return scrapling_browser_scrape(url)
        return firecrawl.scrape(url)

    terms_pages: dict[str, str] = {}
    terms_lock = threading.Lock()

    def terms_html(url: str) -> str:
        # 同一批次中多个页面链接同一条款页时只抓一次
        with terms_lock:
            if url not in terms_pages:
                data = (firecrawl.scrape(url, ("markdown", "rawHtml"), only_main_content=False) if firecrawl
                        else fetch(url))
                terms_pages[url] = str(data.get("rawHtml") or data.get("html") or "")
            return terms_pages[url]

    def scrape(url: str) -> str:
        fingerprint = scrape_request_fingerprint(url, only_main_content=True)
        try:
            data = fetch(url)
            # 卡片链接到的条款段落（条件、数量与活动日期常只写在条款页上）；条款页抓不到时页面证据不完整，按抓取失败处理
            linked_terms = collect_linked_terms(str(data.get("markdown") or ""), url, terms_html)
        except (requests.HTTPError, BrowserScrapeFailed) as exc:
            # 页面抓取失败是该页面的结果，记录后由 retry-pages 处理
            save_scrape_response(store, fingerprint, url, None, success=False, error_message=str(exc)[:300])
            return "failed"
        markdown = prepare_scrape_markdown(str(data.get("markdown") or ""))
        if not markdown:
            save_scrape_response(store, fingerprint, url, data, success=False, error_message="empty_markdown")
            return "empty"
        if is_hub_url(url):
            # 促销中心页补回页头区域（导航、通知卡片）里的全站横幅优惠，正文整理时它们随导航一起被删掉
            banners = [block for block in site_banner_blocks(str(data.get("markdown") or ""))
                       if not any(line in markdown for line in block.splitlines() if banner_offer_line(line))]
            if banners:
                markdown = f"{markdown}\n\n## Site-wide offers\n\n" + "\n\n".join(banners)
        payload = {**data, "markdown": markdown}
        if linked_terms:
            payload["linked_terms"] = linked_terms
            print(f"linked terms {url}: {len(linked_terms)}")
        raw_images = data.get("images") or image_urls_from_markdown(markdown)
        image_urls = filter_promo_image_urls([
            str(image.get("url") or image.get("src") or "") if isinstance(image, dict) else str(image)
            for image in raw_images
        ])
        membership_image = any(any(term in image.lower() for term in ("membership", "benefit", "plan"))
                               for image in image_urls)
        record = decisions_by_url[url]
        if record.get("kind") == "membership" or membership_image or record.get("ocr_required") is True:
            markdown_ocr, ocr_meta = build_markdown_ocr(image_urls) if image_urls else ("", [])
            payload.update({"images": image_urls, "markdown_ocr": markdown_ocr or None, "ocr_meta": ocr_meta})
            print(f"image OCR {url}: images={len(image_urls)} readable={sum(bool(item.get('n_lines')) for item in ocr_meta)}")
        save_scrape_response(store, fingerprint, url, payload, success=True)
        return "ok"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        outcomes = list(pool.map(scrape, urls))
    print({name: outcomes.count(name) for name in set(outcomes)}, "urls", len(urls))
    return 0
