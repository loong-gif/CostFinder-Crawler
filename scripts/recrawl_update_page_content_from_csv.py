#!/usr/bin/env python3
"""
Recrawl subpage_url values from CSV via crawl4ai and update promo_website_staging.page_content.
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

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode
from crawl4ai.async_crawler_strategy import AsyncHTTPCrawlerStrategy

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
TABLE = "promo_website_staging"
PAGE_SIZE = 1000

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.promo_site_crawler import prepare_page_content
from crawler.jina_reader_client import JinaReaderClient


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.raw_base_url = base_url.rstrip("/")
        self.service_role_key = service_role_key
        self.base_url = self.raw_base_url + "/rest/v1"
        self.session = requests.Session()
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
    parser = argparse.ArgumentParser(description="Recrawl CSV subpage_url and update promo_website_staging.page_content")
    parser.add_argument("--csv", required=True, help="Input CSV path (must include subpage_url)")
    parser.add_argument("--crawl-concurrency", type=int, default=6, help="crawl4ai 并发数（默认: 6）")
    parser.add_argument("--update-workers", type=int, default=10, help="Supabase 更新并发线程数（默认: 10）")
    parser.add_argument("--fallback-jina", action="store_true", help="对 crawl4ai 失败项使用 Jina Reader 二次抓取")
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
    deduped: Dict[str, bool] = {}
    for url in urls:
        deduped[url] = True
    return list(deduped.keys())


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


async def recrawl_urls(urls: List[str], *, concurrency: int, fallback_jina: bool) -> List[Dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))
    run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=90000)
    results: List[Dict[str, Any]] = []
    jina_client = JinaReaderClient() if fallback_jina else None

    async def worker(crawler: AsyncWebCrawler, url: str) -> None:
        async with semaphore:
            item: Dict[str, Any] = {"subpage_url": url}
            try:
                res = await crawler.arun(url=url, config=run_cfg)
                md = ""
                if getattr(res, "markdown", None) is not None:
                    md = getattr(res.markdown, "fit_markdown", "") or getattr(res.markdown, "raw_markdown", "") or ""
                prep = prepare_page_content(md, source_type="markdown")
                page_content = prep.get("page_content_llm", "") or ""
                success = bool(getattr(res, "success", False)) and bool(str(page_content).strip())
                source_engine = "crawl4ai"
                fallback_used = False
                error_message = getattr(res, "error_message", "")
                status_code = getattr(res, "status_code", None)

                if not success and jina_client is not None:
                    try:
                        page = await jina_client.fetch(url)
                        fallback_prep = prepare_page_content(page.content or "", source_type="markdown")
                        fallback_content = fallback_prep.get("page_content_llm", "") or ""
                        if str(fallback_content).strip():
                            page_content = fallback_content
                            success = True
                            source_engine = "jina_reader"
                            fallback_used = True
                            error_message = ""
                    except Exception as fallback_exc:  # noqa: BLE001
                        if error_message:
                            error_message = f"{error_message}; jina_fallback_error={fallback_exc}"
                        else:
                            error_message = f"jina_fallback_error={fallback_exc}"

                item.update(
                    {
                        "success": success,
                        "status_code": status_code,
                        "error_message": error_message,
                        "page_content": page_content,
                        "domain_name": normalize_domain(urlsplit(url).netloc),
                        "source_engine": source_engine,
                        "fallback_used": fallback_used,
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
                        "source_engine": "crawl4ai",
                        "fallback_used": False,
                    }
                )
            results.append(item)

    strategy = AsyncHTTPCrawlerStrategy()
    async with AsyncWebCrawler(crawler_strategy=strategy) as crawler:
        await asyncio.gather(*(worker(crawler, url) for url in urls))

    return results


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
    return OUTPUT_DIR / f"promo_website_staging_recrawl_update_{timestamp}.json"


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    urls = read_csv_urls(csv_path)
    if args.max_urls and args.max_urls > 0:
        urls = urls[: args.max_urls]

    crawl_results = asyncio.run(
        recrawl_urls(urls, concurrency=args.crawl_concurrency, fallback_jina=args.fallback_jina)
    )
    client = load_client()
    existing_map = fetch_existing_map(client)
    now_iso = datetime.now(timezone.utc).isoformat()

    to_update: List[Dict[str, Any]] = []
    to_insert: List[Dict[str, Any]] = []
    crawl_failures: List[Dict[str, Any]] = []
    fallback_success_rows = 0
    crawl4ai_success_rows = 0

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

        if row.get("source_engine") == "jina_reader":
            fallback_success_rows += 1
        else:
            crawl4ai_success_rows += 1

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
        "crawl4ai_success_rows": crawl4ai_success_rows,
        "jina_fallback_success_rows": fallback_success_rows,
        "fallback_jina_enabled": bool(args.fallback_jina),
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
