#!/usr/bin/env python3
"""
基于 Jina Reader API 抓取站内价格/促销页并导出 promo_website_staging CSV
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_DIR
from crawler.promo_site_crawler import PromoSiteCrawler, build_target_sites
from utils.logger import log

CSV_FIELDS = [
    "promo_website_id",
    "crawl_timestamp",
    "subpage_url",
    "page_content",
    "page_segments_raw",
    "page_segments_filtered",
    "page_content_llm",
    "content_quality_flags",
    "domain_name",
    "processed_status",
    "name",
]


class SupabaseRestClient:
    """简易 Supabase PostgREST 客户端"""

    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
            }
        )

    def fetch_all(self, table: str, select: str, *, page_size: int = 1000) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        start = 0
        while True:
            end = start + page_size - 1
            response = self.session.get(
                f"{self.base_url}/{table}",
                params={"select": select},
                headers={"Range": f"{start}-{end}"},
                timeout=30,
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
            start += page_size
        return rows


def resolve_output_path(custom_output: str | None) -> Path:
    if custom_output:
        return Path(custom_output).expanduser().resolve()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"promo_website_staging_export_{timestamp}.csv"


def write_csv(rows: Iterable[Dict[str, Any]], output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Jina Reader API 抓取站内价格/促销页并导出 CSV")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个目标站点")
    parser.add_argument("--start-from", type=int, default=1, help="从第几个目标站点开始处理（1-based）")
    parser.add_argument("--output", type=str, default=None, help="输出 CSV 路径")
    parser.add_argument("--concurrency", type=int, default=3, help="站点并发数")
    parser.add_argument("--headless", dest="headless", action="store_true", help="兼容参数（Jina Reader 模式下忽略）")
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
        help="兼容参数（Jina Reader 模式下忽略）",
    )
    parser.set_defaults(headless=None)
    return parser.parse_args()


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv()
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


async def run_crawl(args: argparse.Namespace) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    client = load_supabase_client()
    master_rows = client.fetch_all(
        "master_business_info",
        "id,business_id,name,website,website_clean,process_flag",
    )
    promo_rows = client.fetch_all("promo_website_staging", "domain_name")
    targets = build_target_sites(master_rows, (row.get("domain_name") for row in promo_rows))

    if args.start_from > 1:
        targets = targets[args.start_from - 1 :]
    if args.limit:
        targets = targets[: args.limit]

    log.info(f"目标站点数: {len(targets)}")
    log.info("抓取引擎: jina reader API (https://r.jina.ai)")
    crawler = PromoSiteCrawler(headless=args.headless, concurrency=max(1, args.concurrency))
    await crawler.start()
    try:
        hits, stats = await crawler.crawl_sites(targets)
    finally:
        await crawler.close()

    return hits, {
        "target_sites": stats.target_sites,
        "successful_sites": stats.successful_sites,
        "failed_sites": stats.failed_sites,
        "zero_hit_sites": stats.zero_hit_sites,
        "hit_pages": stats.hit_pages,
        "page_failures": stats.page_failures,
    }


def main():
    args = parse_args()
    output_path = resolve_output_path(args.output)
    hits, stats = asyncio.run(run_crawl(args))
    write_csv(hits, output_path)

    log.info(f"CSV 已保存: {output_path}")
    log.info(
        "汇总: 目标站点={target_sites}, 成功站点={successful_sites}, 失败站点={failed_sites}, "
        "零命中站点={zero_hit_sites}, 命中页数={hit_pages}, 页面失败数={page_failures}".format(**stats)
    )


if __name__ == "__main__":
    main()
