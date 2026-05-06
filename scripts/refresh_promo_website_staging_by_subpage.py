#!/usr/bin/env python3
"""
按 promo_website_staging.subpage_url 逐行调用 Apify actor 并回写 page_content。

规则：
- 输入 URL 使用 subpage_url
- 当行 needs_ocr=true 时，传递 needs_ocr 给 actor，优先走 OCR 流程
- 默认全量执行；可用 --limit 先做小样本测试
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_DIR

DEFAULT_ACTOR_ID = "06tTiNomvlvwWR5cm"
DEFAULT_MAX_CRAWL_PAGES = 5
DEFAULT_ACTOR_TIMEOUT_SECS = 1800
REPORT_PREFIX = "promo_website_staging_subpage_refresh"


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
        offset: Optional[int] = None,
        order: Optional[str] = None,
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
        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def update_row(self, table: str, filters: Dict[str, str], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = self.session.patch(
            f"{self.base_url}/{table}",
            params=filters,
            headers={"Prefer": "return=representation"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()


@dataclass(frozen=True)
class TargetRow:
    promo_website_id: int
    subpage_url: str
    needs_ocr: bool
    old_page_content: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按 subpage_url 全量刷新 promo_website_staging.page_content")
    parser.add_argument("--actor-id", default=DEFAULT_ACTOR_ID, help="远端 Apify actor ID")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 条记录")
    parser.add_argument("--offset", type=int, default=0, help="处理起始偏移量")
    parser.add_argument("--id", type=int, default=None, help="仅处理指定 promo_website_id")
    parser.add_argument("--needs-ocr-only", action="store_true", help="仅处理 needs_ocr=true 的记录")
    parser.add_argument("--dry-run", action="store_true", help="只抓取并输出报告，不写回 Supabase")
    parser.add_argument("--max-crawl-pages", type=int, default=DEFAULT_MAX_CRAWL_PAGES, help="单条 actor 最大抓取页数")
    parser.add_argument("--actor-timeout-secs", type=int, default=DEFAULT_ACTOR_TIMEOUT_SECS, help="单条 actor 超时时间")
    return parser.parse_args()


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_report_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"{REPORT_PREFIX}_{timestamp}.json"


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def run_cli_json(command: List[str]) -> Dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "命令执行失败")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"命令输出不是合法 JSON: {exc}") from exc


def run_actor(
    actor_id: str,
    subpage_url: str,
    needs_ocr: bool,
    max_crawl_pages: int,
    actor_timeout_secs: int,
) -> Dict[str, Any]:
    actor_input = {
        "subpage_url": subpage_url,
        "website_url": subpage_url,
        "start_urls": [
            {
                "url": subpage_url,
                "needs_ocr": bool(needs_ocr),
            }
        ],
        "needs_ocr": bool(needs_ocr),
        "maxCrawlPages": max_crawl_pages,
    }
    with NamedTemporaryFile("w", suffix=".json", delete=True, encoding="utf-8") as handle:
        json.dump(actor_input, handle, ensure_ascii=False)
        handle.flush()
        return run_cli_json(
            [
                "apify",
                "actors",
                "call",
                actor_id,
                "--input-file",
                handle.name,
                "--silent",
                "--json",
                "--timeout",
                str(actor_timeout_secs),
            ]
        )


def fetch_dataset_items(dataset_id: str) -> List[Dict[str, Any]]:
    result = subprocess.run(
        ["apify", "datasets", "get-items", dataset_id, "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "拉取 dataset 失败")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dataset 输出不是合法 JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError("dataset 输出格式异常，预期为 JSON 数组")
    return payload


def pick_page_content(items: List[Dict[str, Any]], target_subpage_url: str) -> str:
    target = (target_subpage_url or "").strip().lower().rstrip("/")
    exact_content = ""
    fallback_content = ""
    for item in items:
        content = (item.get("page_content") or "").strip()
        if not content:
            continue
        if not fallback_content:
            fallback_content = content
        url = (item.get("subpage_url") or "").strip().lower().rstrip("/")
        if target and url and url == target:
            exact_content = content
            break
    return exact_content or fallback_content


def fetch_targets(
    client: SupabaseRestClient,
    *,
    offset: int,
    limit: Optional[int],
    only_id: Optional[int],
    needs_ocr_only: bool,
) -> tuple[List[TargetRow], bool]:
    filters: Dict[str, str] = {"subpage_url": "not.is.null"}
    if only_id is not None:
        filters["promo_website_id"] = f"eq.{only_id}"
    if needs_ocr_only:
        filters["needs_ocr"] = "eq.true"

    select_with_ocr = "promo_website_id,subpage_url,page_content,needs_ocr"
    select_without_ocr = "promo_website_id,subpage_url,page_content"
    has_needs_ocr_column = True
    try:
        rows = client.fetch_rows(
            "promo_website_staging",
            select_with_ocr,
            filters=filters,
            order="promo_website_id.asc",
            offset=offset,
            limit=limit,
        )
    except requests.HTTPError as exc:
        body = ""
        try:
            body = exc.response.text
        except Exception:
            body = ""
        if "needs_ocr" not in body:
            raise
        if needs_ocr_only:
            raise RuntimeError("当前表缺少 needs_ocr 字段，无法使用 --needs-ocr-only 过滤")
        has_needs_ocr_column = False
        rows = client.fetch_rows(
            "promo_website_staging",
            select_without_ocr,
            filters=filters,
            order="promo_website_id.asc",
            offset=offset,
            limit=limit,
        )

    targets: List[TargetRow] = []
    for row in rows:
        subpage_url = str(row.get("subpage_url") or "").strip()
        if not subpage_url:
            continue
        row_id = row.get("promo_website_id")
        if row_id is None:
            continue
        targets.append(
            TargetRow(
                promo_website_id=int(row_id),
                subpage_url=subpage_url,
                needs_ocr=bool(row.get("needs_ocr", False)),
                old_page_content=str(row.get("page_content") or ""),
            )
        )
    return targets, has_needs_ocr_column


def main() -> None:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    client = load_supabase_client()
    targets, has_needs_ocr_column = fetch_targets(
        client,
        offset=max(0, args.offset),
        limit=args.limit,
        only_id=args.id,
        needs_ocr_only=bool(args.needs_ocr_only),
    )
    if not targets:
        raise RuntimeError("没有可处理的 promo_website_staging 记录")

    report_rows: List[Dict[str, Any]] = []
    updated_rows = 0
    success_rows = 0
    error_rows = 0
    started_at = datetime.now(timezone.utc).isoformat()

    for index, target in enumerate(targets, start=1):
        try:
            run_info = run_actor(
                actor_id=args.actor_id,
                subpage_url=target.subpage_url,
                needs_ocr=target.needs_ocr,
                max_crawl_pages=args.max_crawl_pages,
                actor_timeout_secs=args.actor_timeout_secs,
            )
            dataset_id = run_info.get("defaultDatasetId")
            if not dataset_id:
                raise RuntimeError("actor 运行缺少 defaultDatasetId")

            actor_items = fetch_dataset_items(dataset_id)
            new_page_content = pick_page_content(actor_items, target.subpage_url)
            if not new_page_content:
                raise RuntimeError("dataset 中未拿到可用 page_content")

            changed = new_page_content != (target.old_page_content or "")
            if not args.dry_run:
                payload = {
                    "page_content": new_page_content,
                    "crawl_timestamp": datetime.now(timezone.utc).isoformat(),
                }
                if changed:
                    payload["processed_status"] = False
                client.update_row(
                    "promo_website_staging",
                    {"promo_website_id": f"eq.{target.promo_website_id}"},
                    payload,
                )
                updated_rows += 1

            success_rows += 1
            report_rows.append(
                {
                    "status": "ok",
                    "index": index,
                    "promo_website_id": target.promo_website_id,
                    "subpage_url": target.subpage_url,
                    "needs_ocr": bool(target.needs_ocr),
                    "dataset_id": dataset_id,
                    "actor_run_id": run_info.get("id"),
                    "dataset_item_count": len(actor_items),
                    "content_changed": bool(changed),
                    "old_content_length": len(target.old_page_content or ""),
                    "new_content_length": len(new_page_content),
                }
            )
        except Exception as exc:
            error_rows += 1
            report_rows.append(
                {
                    "status": "error",
                    "index": index,
                    "promo_website_id": target.promo_website_id,
                    "subpage_url": target.subpage_url,
                    "needs_ocr": bool(target.needs_ocr),
                    "error": str(exc),
                }
            )

    report_path = resolve_report_path()
    summary = {
        "status": "completed",
        "started_at": started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(args.dry_run),
        "actor_id": args.actor_id,
        "offset": args.offset,
        "limit": args.limit,
        "id": args.id,
        "needs_ocr_only": bool(args.needs_ocr_only),
        "target_rows": len(targets),
        "success_rows": success_rows,
        "error_rows": error_rows,
        "updated_rows": updated_rows,
        "has_needs_ocr_column": has_needs_ocr_column,
        "report_path": str(report_path),
    }
    write_json(report_path, {**summary, "rows": report_rows})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
