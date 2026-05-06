#!/usr/bin/env python3
"""
Update promo_website_staging.page_content in Supabase to the LLM-ready format.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_DIR
from utils.offer_extraction_llm import build_text_segments
from crawler.promo_site_crawler import build_llm_ready_content, filter_page_segments


SEGMENT_PREFIX_PATTERN = re.compile(r"^(?:\s*\[SEGMENT\s+\d+\]\s*)+", re.IGNORECASE)


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        order: Optional[str] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"select": select}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        if order:
            params["order"] = order
        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def update_row(self, table: str, filters: Dict[str, str], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = self.session.patch(
            f"{self.base_url}/{table}",
            params=filters,
            headers={"Prefer": "return=representation"},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def build_llm_ready_page_content(raw_page_content: str) -> str:
    raw_text = (raw_page_content or "").strip()
    if not raw_text:
        return ""

    if raw_text.startswith("[SEGMENT 0]"):
        normalized_blocks = []
        for part in re.split(r"\n{2,}", raw_text):
            cleaned = SEGMENT_PREFIX_PATTERN.sub("", part.strip())
            if cleaned:
                normalized_blocks.append(cleaned)
        if normalized_blocks:
            return "\n\n".join(f"[SEGMENT {idx}] {text}" for idx, text in enumerate(normalized_blocks))
        return raw_text

    filtered_segments, _ = filter_page_segments(build_text_segments(raw_text))
    page_content_llm = build_llm_ready_content(filtered_segments)
    if not page_content_llm:
        return raw_text[:6000]
    return page_content_llm


def resolve_report_path(record_id: int) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"supabase_page_content_update_test_{record_id}_{timestamp}.json"


def resolve_batch_report_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"supabase_page_content_batch_update_{timestamp}.json"


def fetch_target_rows(
    client: SupabaseRestClient,
    *,
    record_id: Optional[int],
    limit: Optional[int],
    page_size: int = 200,
) -> List[Dict[str, Any]]:
    filters = {"page_content": "not.is.null"}
    if record_id is not None:
        filters["promo_website_id"] = f"eq.{record_id}"
        rows = client.fetch_rows(
            "promo_website_staging",
            "promo_website_id,domain_name,subpage_url,page_content",
            filters=filters,
            limit=1,
            order="promo_website_id.asc",
        )
        if not rows:
            raise RuntimeError("未找到可更新的 promo_website_staging 记录")
        return rows

    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(
            "promo_website_staging",
            "promo_website_id,domain_name,subpage_url,page_content",
            filters=filters,
            limit=page_size,
            offset=offset,
            order="promo_website_id.asc",
        )
        if not batch:
            break
        rows.extend(batch)
        if limit is not None and len(rows) >= limit:
            return rows[:limit]
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def summarize_row(row: Dict[str, Any], *, dry_run: bool) -> Dict[str, Any]:
    old_content = row.get("page_content") or ""
    new_content = build_llm_ready_page_content(old_content)
    return {
        "promo_website_id": row["promo_website_id"],
        "domain_name": row.get("domain_name", ""),
        "subpage_url": row.get("subpage_url", ""),
        "old_length": len(old_content),
        "new_length": len(new_content),
        "changed": old_content != new_content,
        "dry_run": dry_run,
        "old_preview": old_content[:1000],
        "new_preview": new_content[:1000],
        "new_page_content": new_content,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把 Supabase 中的 page_content 更新为 llm-ready 版本")
    parser.add_argument("--promo-website-id", type=int, default=None, help="指定要更新的 promo_website_id")
    parser.add_argument("--limit", type=int, default=None, help="批量模式下仅处理前 N 条记录")
    parser.add_argument("--dry-run", action="store_true", help="仅生成报告，不写回 Supabase")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = load_supabase_client()
    rows = fetch_target_rows(client, record_id=args.promo_website_id, limit=args.limit)
    summaries = [summarize_row(row, dry_run=bool(args.dry_run)) for row in rows]

    total_rows = len(summaries)
    changed_rows = sum(1 for item in summaries if item["changed"])
    updated_rows = 0
    if not args.dry_run:
        for item in summaries:
            if not item["changed"]:
                continue
            client.update_row(
                "promo_website_staging",
                {"promo_website_id": f"eq.{item['promo_website_id']}"},
                {"page_content": item["new_page_content"]},
            )
            updated_rows += 1

    if args.promo_website_id is not None:
        report_path = resolve_report_path(int(args.promo_website_id))
    else:
        report_path = resolve_batch_report_path()

    report = {
        "dry_run": bool(args.dry_run),
        "single_record_mode": args.promo_website_id is not None,
        "requested_limit": args.limit,
        "total_rows": total_rows,
        "changed_rows": changed_rows,
        "updated_rows": updated_rows,
        "rows": [
            {
                key: value
                for key, value in item.items()
                if key != "new_page_content"
            }
            for item in summaries
        ],
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report_path": str(report_path),
                "dry_run": report["dry_run"],
                "single_record_mode": report["single_record_mode"],
                "requested_limit": report["requested_limit"],
                "total_rows": report["total_rows"],
                "changed_rows": report["changed_rows"],
                "updated_rows": report["updated_rows"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
