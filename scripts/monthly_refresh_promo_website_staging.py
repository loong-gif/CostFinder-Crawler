#!/usr/bin/env python3
"""
Monthly refresh for promo_website_staging using the remote Apify actor.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_DIR
from crawler.promo_site_crawler import SiteTarget, build_start_url, is_filtered_process_flag, normalize_domain

DEFAULT_ACTOR_ID = "06tTiNomvlvwWR5cm"
DEFAULT_MAX_CRAWL_PAGES = 50
DEFAULT_ACTOR_TIMEOUT_SECS = 1800
REPORT_PREFIX = "monthly_promo_website_refresh"
SKIP_REPORT_PREFIX = "monthly_promo_website_refresh_skip"


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

    def insert_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        response = self.session.post(
            f"{self.base_url}/{table}",
            headers={"Prefer": "return=representation"},
            json=rows,
            timeout=60,
        )
        response.raise_for_status()
        return response.json()


@dataclass(frozen=True)
class SyncTarget:
    domain_name: str
    website_url: str
    name: str
    master_id: Optional[int]
    business_id: Optional[int]


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按月调用 Apify actor 刷新 promo_website_staging.page_content")
    parser.add_argument("--actor-id", default=DEFAULT_ACTOR_ID, help="远端 Apify actor ID")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个域名")
    parser.add_argument("--domain", default=None, help="只处理指定域名")
    parser.add_argument("--max-crawl-pages", type=int, default=DEFAULT_MAX_CRAWL_PAGES, help="单站 actor 最大抓取页数")
    parser.add_argument("--actor-timeout-secs", type=int, default=DEFAULT_ACTOR_TIMEOUT_SECS, help="单站 actor 超时时间")
    parser.add_argument("--dry-run", action="store_true", help="只抓取并生成报告，不写回 Supabase")
    parser.add_argument(
        "--once-per-month",
        action="store_true",
        help="如果本月已存在成功报告，则直接跳过，适合挂到每周自动化里",
    )
    return parser.parse_args()


def resolve_report_path(prefix: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"{prefix}_{timestamp}.json"


def has_completed_report_for_current_month() -> bool:
    month_prefix = datetime.now().strftime("%Y%m")
    pattern = f"{REPORT_PREFIX}_{month_prefix}*.json"
    for path in OUTPUT_DIR.glob(pattern):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("status") == "completed":
            return True
    return False


def fetch_all_rows(
    client: SupabaseRestClient,
    table: str,
    select: str,
    *,
    filters: Optional[Dict[str, str]] = None,
    page_size: int = 500,
    order: Optional[str] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(
            table,
            select,
            filters=filters,
            limit=page_size,
            offset=offset,
            order=order,
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows


def build_sync_targets(client: SupabaseRestClient) -> List[SyncTarget]:
    promo_rows = fetch_all_rows(
        client,
        "promo_website_staging",
        "domain_name,name",
        filters={"domain_name": "not.is.null"},
        order="domain_name.asc",
    )
    master_rows = fetch_all_rows(
        client,
        "master_business_info",
        "id,business_id,name,website,website_clean,process_flag",
        order="id.asc",
    )

    promo_name_by_domain: Dict[str, str] = {}
    promo_domains: List[str] = []
    for row in promo_rows:
        domain_name = normalize_domain(row.get("domain_name"))
        if not domain_name or domain_name in promo_name_by_domain:
            continue
        promo_name_by_domain[domain_name] = (row.get("name") or "").strip()
        promo_domains.append(domain_name)

    master_by_domain: Dict[str, Dict[str, Any]] = {}
    for row in master_rows:
        domain_name = normalize_domain(row.get("website_clean") or row.get("website"))
        if not domain_name or domain_name in master_by_domain:
            continue
        if is_filtered_process_flag(row.get("process_flag")):
            continue
        master_by_domain[domain_name] = row

    targets: List[SyncTarget] = []
    for domain_name in sorted(set(promo_domains)):
        master_row = master_by_domain.get(domain_name)
        if master_row:
            site = SiteTarget(
                master_id=master_row.get("id"),
                business_id=master_row.get("business_id"),
                name=(master_row.get("name") or promo_name_by_domain.get(domain_name) or "").strip(),
                website=(master_row.get("website") or "").strip(),
                website_clean=(master_row.get("website_clean") or "").strip(),
                process_flag=(master_row.get("process_flag") or "").strip(),
                domain_name=domain_name,
            )
            website_url = normalize_seed_url(build_start_url(site) or f"https://{domain_name}")
            targets.append(
                SyncTarget(
                    domain_name=domain_name,
                    website_url=website_url,
                    name=site.name,
                    master_id=site.master_id,
                    business_id=site.business_id,
                )
            )
            continue

        targets.append(
            SyncTarget(
                domain_name=domain_name,
                website_url=normalize_seed_url(f"https://{domain_name}"),
                name=promo_name_by_domain.get(domain_name, ""),
                master_id=None,
                business_id=None,
            )
        )
    return targets


def normalize_seed_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    clean = parsed._replace(query="", fragment="")
    return urlunparse(clean)


def canonicalize_page_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = normalize_domain(parsed.netloc or parsed.path.split("/")[0])
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    clean = parsed._replace(netloc=host, path=path, query="", fragment="")
    return urlunparse(clean)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_cli_json(command: List[str]) -> Dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "命令执行失败")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"命令输出不是合法 JSON: {exc}") from exc


def run_actor(actor_id: str, website_url: str, max_crawl_pages: int, actor_timeout_secs: int) -> Dict[str, Any]:
    actor_input = {
        # Prefer subpage_url for newer actor input contracts.
        # Keep website_url for backward compatibility.
        "subpage_url": website_url,
        "website_url": website_url,
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


def normalize_actor_items(items: Iterable[Dict[str, Any]], target: SyncTarget) -> List[Dict[str, Any]]:
    crawl_timestamp = datetime.now(timezone.utc).isoformat()
    normalized: Dict[str, Dict[str, Any]] = {}
    for item in items:
        subpage_url = (item.get("subpage_url") or "").strip()
        page_content = (item.get("page_content") or "").strip()
        if not subpage_url or not page_content:
            continue
        canonical_url = canonicalize_page_url(subpage_url)
        normalized[canonical_url or subpage_url] = {
            "crawl_timestamp": crawl_timestamp,
            "subpage_url": subpage_url,
            "page_content": page_content,
            "domain_name": normalize_domain(item.get("domain") or target.domain_name) or target.domain_name,
            "processed_status": False,
            "name": (item.get("name") or target.name or "").strip() or None,
        }
    return list(normalized.values())


def sync_domain_rows(
    client: SupabaseRestClient,
    target: SyncTarget,
    actor_rows: List[Dict[str, Any]],
    existing_rows: List[Dict[str, Any]],
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    existing_by_url = {
        canonicalize_page_url(row.get("subpage_url") or ""): row
        for row in existing_rows
        if canonicalize_page_url(row.get("subpage_url") or "")
    }

    to_update: List[Dict[str, Any]] = []
    to_insert: List[Dict[str, Any]] = []
    unchanged = 0

    for row in actor_rows:
        existing = existing_by_url.get(canonicalize_page_url(row["subpage_url"]))
        if existing is None:
            to_insert.append(row)
            continue

        payload = {
            "crawl_timestamp": row["crawl_timestamp"],
            "page_content": row["page_content"],
            "name": row["name"],
        }
        changed = (
            (existing.get("page_content") or "") != row["page_content"]
            or (existing.get("name") or None) != row["name"]
        )
        if changed:
            payload["processed_status"] = False
            to_update.append(
                {
                    "promo_website_id": existing["promo_website_id"],
                    "payload": payload,
                    "changed": True,
                    "subpage_url": row["subpage_url"],
                }
            )
        else:
            to_update.append(
                {
                    "promo_website_id": existing["promo_website_id"],
                    "payload": {"crawl_timestamp": row["crawl_timestamp"]},
                    "changed": False,
                    "subpage_url": row["subpage_url"],
                }
            )
            unchanged += 1

    updated_rows = 0
    inserted_rows = 0
    if not dry_run:
        for item in to_update:
            client.update_row(
                "promo_website_staging",
                {"promo_website_id": f"eq.{item['promo_website_id']}"},
                item["payload"],
            )
            updated_rows += 1
        if to_insert:
            client.insert_rows("promo_website_staging", to_insert)
            inserted_rows = len(to_insert)

    return {
        "domain_name": target.domain_name,
        "website_url": target.website_url,
        "existing_rows": len(existing_rows),
        "actor_rows": len(actor_rows),
        "matched_rows": len(to_update),
        "content_changed_rows": sum(1 for item in to_update if item["changed"]),
        "timestamp_only_rows": unchanged,
        "insert_rows": len(to_insert),
        "updated_rows": updated_rows,
        "inserted_rows": inserted_rows,
        "sample_subpage_urls": [row["subpage_url"] for row in actor_rows[:5]],
    }


def main() -> None:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.once_per_month and has_completed_report_for_current_month():
        report_path = resolve_report_path(SKIP_REPORT_PREFIX)
        report = {
            "status": "skipped_existing_month_report",
            "dry_run": bool(args.dry_run),
            "actor_id": args.actor_id,
            "limit": args.limit,
            "domain": args.domain,
            "message": "本月已经存在成功执行报告，跳过刷新。",
        }
        write_json(report_path, report)
        print(json.dumps({"report_path": str(report_path), **report}, ensure_ascii=False, indent=2))
        return

    client = load_supabase_client()
    targets = build_sync_targets(client)
    all_existing_rows = fetch_all_rows(
        client,
        "promo_website_staging",
        "promo_website_id,domain_name,subpage_url,page_content,crawl_timestamp,processed_status,name",
        order="promo_website_id.asc",
    )
    existing_rows_by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for row in all_existing_rows:
        normalized_domain = normalize_domain(row.get("domain_name"))
        if not normalized_domain:
            continue
        existing_rows_by_domain.setdefault(normalized_domain, []).append(row)
    if args.domain:
        normalized_domain = normalize_domain(args.domain)
        targets = [target for target in targets if target.domain_name == normalized_domain]
    if args.limit is not None:
        targets = targets[: args.limit]
    if not targets:
        raise RuntimeError("没有可处理的目标域名")

    domain_reports: List[Dict[str, Any]] = []
    total_actor_rows = 0
    total_errors = 0
    total_updates = 0
    total_inserts = 0

    for index, target in enumerate(targets, start=1):
        try:
            run_info = run_actor(
                args.actor_id,
                website_url=target.website_url,
                max_crawl_pages=args.max_crawl_pages,
                actor_timeout_secs=args.actor_timeout_secs,
            )
            dataset_id = run_info.get("defaultDatasetId")
            if not dataset_id:
                raise RuntimeError("actor 运行缺少 defaultDatasetId")
            actor_items = fetch_dataset_items(dataset_id)
            actor_rows = normalize_actor_items(actor_items, target)
            total_actor_rows += len(actor_rows)
            sync_report = sync_domain_rows(
                client,
                target,
                actor_rows,
                existing_rows_by_domain.get(target.domain_name, []),
                dry_run=bool(args.dry_run),
            )
            total_updates += sync_report["updated_rows"]
            total_inserts += sync_report["inserted_rows"]
            domain_reports.append(
                {
                    "status": "ok",
                    "index": index,
                    "actor_run_id": run_info.get("id"),
                    "dataset_id": dataset_id,
                    "target": asdict(target),
                    "sync": sync_report,
                }
            )
        except Exception as exc:
            total_errors += 1
            domain_reports.append(
                {
                    "status": "error",
                    "index": index,
                    "target": asdict(target),
                    "error": str(exc),
                }
            )

    report_path = resolve_report_path(REPORT_PREFIX)
    summary = {
        "status": "completed",
        "dry_run": bool(args.dry_run),
        "actor_id": args.actor_id,
        "limit": args.limit,
        "domain": normalize_domain(args.domain) if args.domain else None,
        "target_domains": len(targets),
        "error_domains": total_errors,
        "total_actor_rows": total_actor_rows,
        "updated_rows": total_updates,
        "inserted_rows": total_inserts,
        "report_path": str(report_path),
    }
    report = {
        **summary,
        "domains": domain_reports,
    }
    write_json(report_path, report)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
