"""爬虫层：用自托管 Firecrawl 抓取已决定加入的页面，原始结果写入本地 raw 文件存储。"""
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from dotenv import load_dotenv

from utils.firecrawl_client import self_hosted_firecrawl
from utils.firecrawl_scrape_raw_db import save_scrape_response, scrape_request_fingerprint
from utils.browser_scrape import BrowserScrapeFailed, ego_scrape, lightpanda_scrape
from utils.raw_file_store import RawFileStore
from utils.scrape_markdown import prepare_scrape_markdown
from utils.paddle_ocr_markdown import build_markdown_ocr, filter_promo_image_urls, image_urls_from_markdown

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decisions", type=Path, default=PROJECT_ROOT / "state/discovery/monitor_decisions_final.json")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--engine", choices=["firecrawl", "lightpanda", "ego"], default="firecrawl",
                        help="自托管 Firecrawl 抓取失败的页面改由 Lightpanda 或本机 ego-browser 渲染后转 markdown")
    parser.add_argument("--url", action="append", help="只抓取决定文件中的这些 URL，可重复")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("COSTFINDER_RAW_DIR"):
        raise RuntimeError("set COSTFINDER_RAW_DIR so the batch stays isolated")
    firecrawl = self_hosted_firecrawl() if args.engine == "firecrawl" else None
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
        if args.engine == "lightpanda":
            return lightpanda_scrape(url)
        if args.engine == "ego":
            return ego_scrape(url)
        return firecrawl.scrape(url)

    def scrape(url: str) -> str:
        fingerprint = scrape_request_fingerprint(url, only_main_content=True)
        try:
            data = fetch(url)
        except (requests.HTTPError, BrowserScrapeFailed) as exc:
            # 页面抓取失败是该页面的结果，记录后由 retry-pages 处理
            save_scrape_response(store, fingerprint, url, None, success=False, error_message=str(exc)[:300])
            return "failed"
        markdown = prepare_scrape_markdown(str(data.get("markdown") or ""))
        if not markdown:
            save_scrape_response(store, fingerprint, url, data, success=False, error_message="empty_markdown")
            return "empty"
        payload = {**data, "markdown": markdown}
        raw_images = data.get("images") or image_urls_from_markdown(markdown)
        image_urls = filter_promo_image_urls([
            str(image.get("url") or image.get("src") or "") if isinstance(image, dict) else str(image)
            for image in raw_images
        ])
        membership_image = any(any(term in image.lower() for term in ("membership", "benefit", "plan"))
                               for image in image_urls)
        if decisions_by_url[url].get("kind") == "membership" or membership_image:
            markdown_ocr, ocr_meta = build_markdown_ocr(image_urls) if image_urls else ("", [])
            payload.update({"images": image_urls, "markdown_ocr": markdown_ocr or None, "ocr_meta": ocr_meta})
            print(f"membership OCR {url}: images={len(image_urls)} readable={sum(bool(item.get('n_lines')) for item in ocr_meta)}")
        save_scrape_response(store, fingerprint, url, payload, success=True)
        return "ok"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        outcomes = list(pool.map(scrape, urls))
    print({name: outcomes.count(name) for name in set(outcomes)}, "urls", len(urls))
    return 0
