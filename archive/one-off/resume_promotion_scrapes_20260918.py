#!/usr/bin/env python3
"""Fetch active promotion sources that currently have no Offer coverage.

This is a read-only evidence collection step. It never changes Supabase rows.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.membership_repair_scrape import FirecrawlHTTP, scrape_one  # noqa: E402
from utils.schema_contract import CLINIC_PROMOTION_SELECT, OFFER_MASTER_SELECT  # noqa: E402
from utils.supabase_access import fetch_all_rows, load_supabase_client  # noqa: E402


def read_json(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_rows() -> list[dict[str, Any]]:
    client = load_supabase_client(ROOT)
    promotions = fetch_all_rows(
        client, "clinic_promotions", CLINIC_PROMOTION_SELECT, order="promotion_id.asc"
    )
    offers = fetch_all_rows(
        client, "promo_offer_master", OFFER_MASTER_SELECT, order="id.asc"
    )
    covered = {row.get("promotion_id") for row in offers if row.get("promotion_id") is not None}
    return [
        row for row in promotions
        if row.get("is_active") is True
        and row.get("promotion_id") not in covered
        and str(row.get("source_url") or "").strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.5)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    source = source_rows()
    source_path = args.artifact_dir / "promotion_sources.json"
    partial_path = args.artifact_dir / "firecrawl_main_scrapes_partial.json"
    output_path = args.artifact_dir / "firecrawl_main_scrapes_resumed.json"
    write_json(source_path, source)
    pages = read_json(partial_path) if partial_path.exists() else []
    by_url = {str(page.get("requested_url")): page for page in pages if page.get("requested_url")}
    urls = [str(row["source_url"]) for row in source]
    missing_urls = [url for url in urls if not by_url.get(url, {}).get("markdown")]

    api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FIRECRAWL_API_KEY is required")
    fc = FirecrawlHTTP(
        os.getenv("COSTFINDER_FIRECRAWL_URL", "http://192.168.1.68:3002"),
        api_key,
        request_interval=args.interval,
    )
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    for index, url in enumerate(missing_urls, start=1):
        page = scrape_one(fc, url)
        by_url[url] = page
        write_json(partial_path, list(by_url.values()))
        print(
            json.dumps(
                {
                    "completed": index,
                    "total": len(missing_urls),
                    "url": url,
                    "ok": bool(page.get("markdown")),
                    "error": page.get("error"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    write_json(output_path, list(by_url.values()))
    print(
        json.dumps(
            {
                "output": str(output_path),
                "source_count": len(source),
                "total_pages": len(by_url),
                "with_markdown": sum(bool(page.get("markdown")) for page in by_url.values()),
                "errors": sum(bool(page.get("error")) for page in by_url.values()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
