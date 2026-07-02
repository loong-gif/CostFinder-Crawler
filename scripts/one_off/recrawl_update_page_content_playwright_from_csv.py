#!/usr/bin/env python3
"""
Recrawl subpage_url values from CSV via Playwright rendered text
and update promo_website_staging.page_content.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv

try:
    from playwright.async_api import async_playwright
except ImportError as playwright_import_error:
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = playwright_import_error
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
TABLE = "promo_website_staging"
PAGE_SIZE = 1000

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.page_content_processor import process_page_content


def ensure_playwright_available() -> None:
    if async_playwright is None:
        raise RuntimeError(
            "该脚本属于可选浏览器补抓工具。请先执行 "
            "`uv pip install -r requirements_browser_tools.txt`，再执行 "
            "`PLAYWRIGHT_BROWSERS_PATH=.playwright_browsers playwright install chromium`。"
        ) from _PLAYWRIGHT_IMPORT_ERROR


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.raw_base_url = base_url.rstrip("/")
        self.service_role_key = service_role_key
        self.base_url = self.raw_base_url + "/rest/v1"
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows(self, table: str, select: str, *, limit: int, offset: int, order: str) -> List[Dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/{table}",
            params={"select": select, "limit": str(limit), "offset": str(offset), "order": order},
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def update_row(self, table: str, row_id: int, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = self.session.patch(
            f"{self.base_url}/{table}",
            params={"promo_website_id": f"eq.{row_id}"},
            headers={"Prefer": "return=representation"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()

    def insert_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        response = self.session.post(
            f"{self.base_url}/{table}",
            headers={"Prefer": "return=representation"},
            json=rows,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()


def normalize_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    if text.startswith("www."):
        text = text[4:]
    return text.strip(".")


def load_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recrawl CSV subpage_url via Playwright and update promo_website_staging.page_content")
    parser.add_argument("--csv", required=True, help="Input CSV path (must include subpage_url)")
    parser.add_argument("--crawl-concurrency", type=int, default=4, help="Playwright 并发数（默认: 4）")
    parser.add_argument("--update-workers", type=int, default=10, help="Supabase 更新并发线程数（默认: 10）")
    parser.add_argument("--timeout-ms", type=int, default=90000, help="页面超时毫秒（默认: 90000）")
    parser.add_argument("--use-lightpanda", action="store_true", help="优先通过 CDP 连接 lightpanda")
    parser.add_argument("--cdp-url", default=os.getenv("BROWSER_CDP_URL", "http://127.0.0.1:9222"), help="CDP endpoint URL")
    parser.add_argument("--allow-local-fallback", action="store_true", help="lightpanda 连接失败时回退到本地 Chrome")
    parser.add_argument("--max-urls", type=int, default=0, help="仅处理前 N 条 URL（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="仅抓取与预览，不写库")
    return parser.parse_args()


def read_csv_urls(csv_path: Path) -> List[str]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        urls: List[str] = []
        for raw in reader:
            subpage_url = str(raw.get("subpage_url") or "").strip()
            if subpage_url:
                urls.append(subpage_url)
    return list(dict.fromkeys(urls))


def fetch_existing_map(client: SupabaseRestClient) -> Dict[str, Dict[str, Any]]:
    existing: Dict[str, Dict[str, Any]] = {}
    offset = 0
    while True:
        batch = client.fetch_rows(
            TABLE,
            "promo_website_id,subpage_url,domain_name,name",
            limit=PAGE_SIZE,
            offset=offset,
            order="promo_website_id.asc",
        )
        if not batch:
            break
        for row in batch:
            subpage_url = str(row.get("subpage_url") or "").strip()
            if subpage_url:
                existing[subpage_url] = row
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return existing


async def recrawl_urls_playwright(
    urls: List[str],
    *,
    concurrency: int,
    timeout_ms: int,
    use_lightpanda: bool,
    cdp_url: str,
    allow_local_fallback: bool,
) -> tuple[List[Dict[str, Any]], str, str]:
    ensure_playwright_available()
    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: List[Dict[str, Any]] = []
    browser_backend = ""
    backend_note = ""

    async with async_playwright() as p:
        launch_errors: List[str] = []
        browser = None

        if use_lightpanda:
            try:
                browser = await p.chromium.connect_over_cdp(cdp_url)
                browser_backend = "lightpanda_cdp"
                backend_note = f"connected:{cdp_url}"
            except Exception as exc:  # noqa: BLE001
                launch_errors.append(f"lightpanda_cdp: {exc}")
                if not allow_local_fallback:
                    raise RuntimeError(" ; ".join(launch_errors))
                backend_note = f"lightpanda_connect_failed_fallback_local:{exc}"

        if browser is None:
            for mode in ("chrome_channel", "bundled_chromium"):
                try:
                    if mode == "chrome_channel":
                        browser = await p.chromium.launch(headless=True, channel="chrome")
                    else:
                        browser = await p.chromium.launch(headless=True)
                    browser_backend = mode
                    if not backend_note:
                        backend_note = f"launched:{mode}"
                    break
                except Exception as exc:  # noqa: BLE001
                    launch_errors.append(f"{mode}: {exc}")
        if browser is None:
            raise RuntimeError(" ; ".join(launch_errors) or "failed to launch browser")

        if browser_backend == "lightpanda_cdp" and browser.contexts:
            context = browser.contexts[0]
        else:
            context = await browser.new_context(ignore_https_errors=True)

        async def worker(url: str) -> None:
            async with semaphore:
                item: Dict[str, Any] = {"subpage_url": url}
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    await page.wait_for_timeout(1200)
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(1000)
                    rendered_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                    rendered_text = str(rendered_text or "")
                    prep = process_page_content(rendered_text, source_type="markdown")
                    page_content = prep.get("page_content_llm", "") or ""
                    item.update(
                        {
                            "success": bool(page_content.strip()),
                            "status_code": 200,
                            "error_message": "",
                            "page_content": page_content,
                            "domain_name": normalize_domain(urlsplit(url).netloc),
                            "source_engine": "playwright_rendered",
                            "rendered_text_len": len(rendered_text),
                            "browser_backend": browser_backend,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    item.update(
                        {
                            "success": False,
                            "status_code": None,
                            "error_message": str(exc),
                            "page_content": "",
                            "domain_name": normalize_domain(urlsplit(url).netloc),
                            "source_engine": "playwright_rendered",
                            "rendered_text_len": 0,
                            "browser_backend": browser_backend,
                        }
                    )
                finally:
                    await page.close()
                results.append(item)

        await asyncio.gather(*(worker(url) for url in urls))
        if browser_backend != "lightpanda_cdp":
            await context.close()
        await browser.close()
    return results, browser_backend, backend_note


def chunked(items: Iterable[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    buf: List[Dict[str, Any]] = []
    for item in items:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def resolve_report_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"promo_website_staging_playwright_recrawl_update_{timestamp}.json"


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    urls = read_csv_urls(csv_path)
    if args.max_urls and args.max_urls > 0:
        urls = urls[: args.max_urls]

    crawl_results, browser_backend, backend_note = asyncio.run(
        recrawl_urls_playwright(
            urls,
            concurrency=args.crawl_concurrency,
            timeout_ms=args.timeout_ms,
            use_lightpanda=args.use_lightpanda,
            cdp_url=args.cdp_url,
            allow_local_fallback=args.allow_local_fallback,
        )
    )
    client = load_client()
    existing_map = fetch_existing_map(client)
    now_iso = datetime.now(timezone.utc).isoformat()

    to_update: List[Dict[str, Any]] = []
    to_insert: List[Dict[str, Any]] = []
    crawl_failures: List[Dict[str, Any]] = []

    for row in crawl_results:
        if not row.get("success") or not str(row.get("page_content") or "").strip():
            crawl_failures.append(
                {
                    "subpage_url": row.get("subpage_url", ""),
                    "status_code": row.get("status_code"),
                    "error_message": row.get("error_message", ""),
                }
            )
            continue

        payload = {
            "crawl_timestamp": now_iso,
            "subpage_url": row["subpage_url"],
            "page_content": row["page_content"],
            "domain_name": row["domain_name"],
            "processed_status": False,
            "name": str((existing_map.get(row["subpage_url"]) or {}).get("name") or ""),
        }
        matched = existing_map.get(row["subpage_url"])
        if matched:
            to_update.append(
                {
                    "promo_website_id": int(matched["promo_website_id"]),
                    "subpage_url": row["subpage_url"],
                    "payload": payload,
                }
            )
        else:
            to_insert.append(payload)

    updated_rows = 0
    inserted_rows = 0
    update_errors: List[Dict[str, Any]] = []

    if not args.dry_run:
        if to_update:
            workers = max(1, int(args.update_workers))

            def _update_worker(item: Dict[str, Any]) -> Dict[str, Any]:
                worker_client = SupabaseRestClient(client.raw_base_url, client.service_role_key)
                try:
                    worker_client.update_row(TABLE, item["promo_website_id"], item["payload"])
                    return {"ok": True, "subpage_url": item["subpage_url"]}
                except Exception as exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "subpage_url": item["subpage_url"],
                        "promo_website_id": item["promo_website_id"],
                        "error": str(exc),
                    }

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_update_worker, item) for item in to_update]
                for future in as_completed(futures):
                    result = future.result()
                    if result.get("ok"):
                        updated_rows += 1
                    else:
                        update_errors.append(result)

        for batch in chunked(to_insert, 100):
            result = client.insert_rows(TABLE, batch)
            inserted_rows += len(result)

    report = {
        "status": "dry_run" if args.dry_run else "completed",
        "table": TABLE,
        "csv_path": str(csv_path),
        "total_input_urls": len(urls),
        "crawl_success_rows": len(crawl_results) - len(crawl_failures),
        "crawl_failure_rows": len(crawl_failures),
        "source_engine": "playwright_rendered",
        "browser_backend": browser_backend,
        "backend_note": backend_note,
        "use_lightpanda_requested": bool(args.use_lightpanda),
        "to_update_rows": len(to_update),
        "to_insert_rows": len(to_insert),
        "updated_rows": updated_rows,
        "inserted_rows": inserted_rows,
        "update_error_count": len(update_errors),
        "update_errors_sample": update_errors[:20],
        "crawl_failures_sample": crawl_failures[:20],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_update_urls": [item["subpage_url"] for item in to_update[:10]],
        "sample_insert_urls": [item["subpage_url"] for item in to_insert[:10]],
    }
    report_path = resolve_report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report_path": str(report_path), **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
