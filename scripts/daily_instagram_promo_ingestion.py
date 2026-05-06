#!/usr/bin/env python3
"""
Run the daily Instagram promo ingestion job.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import warnings
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+.*")

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_DIR
from crawler.promo_site_crawler import is_filtered_process_flag
from utils.instagram_promo_filter import (
    normalize_instagram_post_url,
    normalize_instagram_profile_url,
    resolve_post_local_date,
    summarize_filtered_post,
)

TABLE_NAME = "promo_social_staging"
DEFAULT_ACTOR_ID = os.getenv("APIFY_INSTAGRAM_ACTOR_ID") or os.getenv("INSTAGRAM_ACTOR_ID") or "apify/instagram-scraper"
DEFAULT_TIMEZONE = os.getenv("TZ") or "Asia/Shanghai"
DEFAULT_RESULTS_LIMIT = 12
DEFAULT_BATCH_SIZE = 25
DEFAULT_ACTOR_TIMEOUT_SECS = 1800
DEFAULT_ONLY_POSTS_NEWER_THAN = "2 days"
DEFAULT_LOOKBACK_DAYS = 1
TIMESTAMP_COLUMN_CANDIDATES = ["local_post_date", "published_at", "posted_at", "timestamp", "crawl_timestamp", "created_at"]
POST_URL_COLUMN_CANDIDATES = ["post_url", "url", "postUrl", "source_url"]


class SupabaseRestClient:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.session = requests.Session()
        # Automation shells may export local proxy env vars that are unreachable
        # in sandboxed runs; bypass them so requests go directly to Supabase.
        self.session.trust_env = False
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
class InstagramTarget:
    master_id: Optional[int]
    business_id: Optional[int]
    name: str
    instagram_url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按日抓取 Instagram 促销内容并写入 promo_social_staging")
    parser.add_argument("--actor-id", default=DEFAULT_ACTOR_ID, help="Apify Instagram actor ID")
    parser.add_argument("--results-limit", type=int, default=DEFAULT_RESULTS_LIMIT, help="每个账号最多拉取的帖子数")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="每次调用 actor 的账号数")
    parser.add_argument("--actor-timeout-secs", type=int, default=DEFAULT_ACTOR_TIMEOUT_SECS, help="单次 actor 调用超时")
    parser.add_argument("--only-posts-newer-than", default=None, help="传给 actor 的只抓取最近帖子窗口，默认按 lookback-days 自动推导")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="用于判定当前本地日期的时区")
    parser.add_argument("--local-date", default=None, help="覆盖当前本地日期，格式 YYYY-MM-DD")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS, help="回看最近 N 个本地自然日，含 local-date 当天")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个 Instagram URL")
    parser.add_argument("--dry-run", action="store_true", help="只生成 artifact，不写入 Supabase")
    parser.add_argument("--fixture-posts-json", default=None, help="离线调试：直接读取本地 actor 输出 JSON")
    parser.add_argument("--fixture-direct-urls-json", default=None, help="离线调试：读取本地 actor 输入 JSON，提取 directUrls")
    return parser.parse_args()


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not service_role_key:
        raise RuntimeError("缺少 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)


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


def resolve_target_date(local_date_arg: Optional[str], timezone_name: str) -> date:
    if local_date_arg:
        return date.fromisoformat(local_date_arg)
    return datetime.now(ZoneInfo(timezone_name)).date()


def resolve_target_date_window(target_date: date, lookback_days: int) -> Tuple[date, date]:
    days = max(1, int(lookback_days or 1))
    return target_date - timedelta(days=days - 1), target_date


def resolve_only_posts_newer_than(args: argparse.Namespace) -> str:
    if args.only_posts_newer_than:
        return args.only_posts_newer_than
    if int(args.lookback_days or 1) <= 1:
        return DEFAULT_ONLY_POSTS_NEWER_THAN
    return f"{int(args.lookback_days)} days"


def resolve_report_path(now: datetime, lookback_days: int) -> Path:
    timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
    mode = "weekly" if int(lookback_days or 1) > 1 else "daily"
    return OUTPUT_DIR / f"instagram_promo_{mode}_ingestion_{timestamp}.json"


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run_cli_json(command: Sequence[str]) -> Dict[str, Any]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "命令执行失败")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"命令输出不是合法 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("命令输出格式异常，预期为 JSON 对象")
    return payload


def run_actor(
    actor_id: str,
    direct_urls: Sequence[str],
    *,
    results_limit: int,
    only_posts_newer_than: str,
    actor_timeout_secs: int,
) -> Dict[str, Any]:
    actor_input = {
        "addParentData": False,
        "directUrls": list(direct_urls),
        "onlyPostsNewerThan": only_posts_newer_than,
        "resultsLimit": results_limit,
        "resultsType": "posts",
        "searchLimit": 1,
        "searchType": "hashtag",
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
    return [item for item in payload if isinstance(item, dict)]


def chunked(items: Sequence[str], size: int) -> Iterator[List[str]]:
    for start in range(0, len(items), max(1, size)):
        yield list(items[start : start + max(1, size)])


def load_fixture_posts(path_str: str) -> List[Dict[str, Any]]:
    path = Path(path_str).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("fixture posts JSON 必须是数组")
    return [item for item in payload if isinstance(item, dict)]


def load_fixture_direct_urls(path_str: str) -> List[str]:
    path = Path(path_str).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("fixture direct URLs JSON 必须是对象")
    raw_urls = payload.get("directUrls") or []
    if not isinstance(raw_urls, list):
        raise RuntimeError("fixture direct URLs JSON 的 directUrls 必须是数组")
    return [normalize_instagram_profile_url(value) for value in raw_urls if normalize_instagram_profile_url(value)]


def build_fixture_targets(posts: Sequence[Dict[str, Any]], fixture_direct_urls_json: Optional[str]) -> List[InstagramTarget]:
    urls: List[str] = []
    if fixture_direct_urls_json:
        urls.extend(load_fixture_direct_urls(fixture_direct_urls_json))
    else:
        for item in posts:
            normalized = normalize_instagram_profile_url(item.get("inputUrl") or "")
            if normalized:
                urls.append(normalized)

    deduped: List[InstagramTarget] = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(InstagramTarget(master_id=None, business_id=None, name="", instagram_url=url))
    return deduped


def enrich_targets_with_master_business_ids(
    client: SupabaseRestClient,
    targets: Sequence[InstagramTarget],
) -> List[InstagramTarget]:
    master_rows = fetch_all_rows(
        client,
        "master_business_info",
        "id,business_id,name,instagram_url,process_flag",
        filters={"instagram_url": "not.is.null"},
        order="id.asc",
    )
    master_by_url: Dict[str, Dict[str, Any]] = {}
    for row in master_rows:
        if is_filtered_process_flag(row.get("process_flag")):
            continue
        normalized_url = normalize_instagram_profile_url(row.get("instagram_url") or "")
        if not normalized_url or normalized_url in master_by_url:
            continue
        master_by_url[normalized_url] = row

    enriched: List[InstagramTarget] = []
    for target in targets:
        master_row = master_by_url.get(target.instagram_url)
        if master_row is None:
            enriched.append(target)
            continue
        enriched.append(
            InstagramTarget(
                master_id=master_row.get("id"),
                business_id=master_row.get("business_id"),
                name=(master_row.get("name") or target.name or "").strip(),
                instagram_url=target.instagram_url,
            )
        )
    return enriched


def fetch_instagram_targets(client: SupabaseRestClient) -> List[InstagramTarget]:
    rows = fetch_all_rows(
        client,
        "master_business_info",
        "id,business_id,name,instagram_url,process_flag",
        filters={"instagram_url": "not.is.null"},
        order="id.asc",
    )
    targets: List[InstagramTarget] = []
    seen_urls = set()
    for row in rows:
        if is_filtered_process_flag(row.get("process_flag")):
            continue
        normalized_url = normalize_instagram_profile_url(row.get("instagram_url") or "")
        if not normalized_url or normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        targets.append(
            InstagramTarget(
                master_id=row.get("id"),
                business_id=row.get("business_id"),
                name=(row.get("name") or "").strip(),
                instagram_url=normalized_url,
            )
        )
    return targets


def detect_table_columns(client: SupabaseRestClient, table: str) -> Set[str]:
    rows = client.fetch_rows(table, "*", limit=1)
    if not rows:
        openapi_response = client.session.get(
            f"{client.base_url}/",
            headers={"Accept": "application/openapi+json"},
            timeout=60,
        )
        openapi_response.raise_for_status()
        spec = openapi_response.json()
        properties = (
            spec.get("definitions", {})
            .get(table, {})
            .get("properties", {})
        )
        return set(properties.keys())
    return set(rows[0].keys())


def build_target_lookup(targets: Sequence[InstagramTarget]) -> Dict[str, InstagramTarget]:
    return {target.instagram_url: target for target in targets}


def local_day_bounds_utc(target_date: date, timezone_name: str) -> Tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    local_start = datetime.combine(target_date, time.min, tzinfo=zone)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def local_date_window_bounds_utc(start_date: date, end_date: date, timezone_name: str) -> Tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    local_start = datetime.combine(start_date, time.min, tzinfo=zone)
    local_end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def stringify_timestamp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


def resolve_existing_row_local_date(row: Dict[str, Any], timezone_name: str) -> str:
    explicit_local_date = (row.get("local_post_date") or "").strip()
    if explicit_local_date:
        return explicit_local_date
    for key in ("published_at", "posted_at", "timestamp", "crawl_timestamp", "created_at"):
        local_value = resolve_post_local_date(stringify_timestamp(row.get(key)), timezone_name=timezone_name)
        if local_value:
            return local_value.isoformat()
    return ""


def resolve_existing_row_post_url(row: Dict[str, Any]) -> str:
    for key in POST_URL_COLUMN_CANDIDATES:
        normalized = normalize_instagram_post_url(row.get(key) or "")
        if normalized:
            return normalized
    return ""


def fetch_existing_post_keys(
    client: SupabaseRestClient,
    *,
    start_date: date,
    end_date: date,
    timezone_name: str,
    available_columns: Set[str],
) -> Set[Tuple[str, str]]:
    if not available_columns:
        return set()

    rows: List[Dict[str, Any]] = []
    if "local_post_date" in available_columns:
        rows = fetch_all_rows(
            client,
            TABLE_NAME,
            "*",
            filters={"and": f"(local_post_date.gte.{start_date.isoformat()},local_post_date.lte.{end_date.isoformat()})"},
        )
    else:
        timestamp_column = next((name for name in TIMESTAMP_COLUMN_CANDIDATES if name in available_columns and name != "local_post_date"), None)
        if timestamp_column:
            utc_start, utc_end = local_date_window_bounds_utc(start_date, end_date, timezone_name)
            rows = fetch_all_rows(
                client,
                TABLE_NAME,
                "*",
                filters={"and": f"({timestamp_column}.gte.{utc_start.isoformat()},{timestamp_column}.lt.{utc_end.isoformat()})"},
            )

    keys: Set[Tuple[str, str]] = set()
    for row in rows:
        post_url = resolve_existing_row_post_url(row)
        local_post_date = resolve_existing_row_local_date(row, timezone_name)
        if post_url and local_post_date:
            keys.add((post_url, local_post_date))
    return keys


def build_base_insert_payload(post: Dict[str, Any], target: Optional[InstagramTarget], run_timestamp: str) -> Dict[str, Any]:
    published_at = stringify_timestamp(post.get("timestamp"))
    input_url = normalize_instagram_profile_url(post.get("inputUrl") or "")
    post_url = normalize_instagram_post_url(post.get("url") or "")
    source_metadata = {
        "inputUrl": input_url or (post.get("inputUrl") or ""),
        "url": post_url or (post.get("url") or ""),
        "ownerUsername": post.get("ownerUsername"),
        "shortCode": post.get("shortCode"),
        "type": post.get("type"),
    }
    return {
        "business_id": target.business_id if target else None,
        "name": target.name if target else None,
        "platform": "instagram",
        "source_platform": "instagram",
        "instagram_url": input_url or (post.get("inputUrl") or ""),
        "input_url": input_url or (post.get("inputUrl") or ""),
        "inputUrl": input_url or (post.get("inputUrl") or ""),
        "post_url": post_url or (post.get("url") or ""),
        "postUrl": post_url or (post.get("url") or ""),
        "url": post_url or (post.get("url") or ""),
        "caption": (post.get("caption") or "").strip(),
        "promo_text_raw": (post.get("caption") or "").strip(),
        "published_at": published_at,
        "posted_at": published_at,
        "timestamp": (post.get("local_post_date") or published_at),
        "crawl_timestamp": run_timestamp,
        "owner_username": post.get("ownerUsername"),
        "ownerUsername": post.get("ownerUsername"),
        "owner_full_name": post.get("ownerFullName"),
        "ownerFullName": post.get("ownerFullName"),
        "short_code": post.get("shortCode"),
        "shortCode": post.get("shortCode"),
        "display_url": post.get("displayUrl"),
        "displayUrl": post.get("displayUrl"),
        "likes_count": post.get("likesCount"),
        "likesCount": post.get("likesCount"),
        "comments_count": post.get("commentsCount"),
        "commentsCount": post.get("commentsCount"),
        "post_type": post.get("type") or post.get("productType"),
        "product_type": post.get("productType"),
        "local_post_date": post.get("local_post_date"),
        "matched_price_signals": json.dumps(post.get("matched_price_signals") or [], ensure_ascii=False),
        "matched_price_signal_labels": json.dumps(post.get("matched_price_signal_labels") or [], ensure_ascii=False),
        "matched_price_signal_count": post.get("matched_price_signal_count"),
        "matched_promo_keyword_labels": json.dumps(post.get("matched_promo_keyword_labels") or [], ensure_ascii=False),
        "matched_weak_labels": json.dumps(post.get("matched_weak_labels") or [], ensure_ascii=False),
        "source_metadata": source_metadata,
        "source_payload": post,
        "raw_payload": post,
        "processed_status": False,
    }


def build_insert_payload_variants(
    post: Dict[str, Any],
    *,
    target: Optional[InstagramTarget],
    run_timestamp: str,
    available_columns: Set[str],
) -> List[Dict[str, Any]]:
    base_payload = build_base_insert_payload(post, target, run_timestamp)
    if available_columns:
        filtered = {key: value for key, value in base_payload.items() if key in available_columns}
        return [filtered]

    snake_case = {
        key: value
        for key, value in base_payload.items()
        if key
        in {
            "business_id",
            "name",
            "platform",
            "instagram_url",
            "input_url",
            "post_url",
            "caption",
            "published_at",
            "crawl_timestamp",
            "owner_username",
            "local_post_date",
            "matched_price_signals",
            "matched_price_signal_labels",
            "matched_price_signal_count",
            "matched_promo_keyword_labels",
            "matched_weak_labels",
            "source_metadata",
            "source_payload",
            "processed_status",
        }
    }
    camel_case = {
        "business_id": base_payload["business_id"],
        "platform": "instagram",
        "inputUrl": base_payload["inputUrl"],
        "postUrl": base_payload["postUrl"],
        "url": base_payload["url"],
        "caption": base_payload["caption"],
        "published_at": base_payload["published_at"],
        "crawl_timestamp": base_payload["crawl_timestamp"],
        "ownerUsername": base_payload["ownerUsername"],
        "local_post_date": base_payload["local_post_date"],
        "source_metadata": base_payload["source_metadata"],
        "raw_payload": base_payload["raw_payload"],
        "processed_status": False,
    }
    return [snake_case, camel_case]


def insert_rows_with_fallback(
    client: SupabaseRestClient,
    *,
    posts: Sequence[Dict[str, Any]],
    target_lookup: Dict[str, InstagramTarget],
    available_columns: Set[str],
    run_timestamp: str,
    dry_run: bool,
) -> Tuple[int, List[Dict[str, Any]], int]:
    if dry_run:
        return 0, [], 0

    insert_errors: List[Dict[str, Any]] = []
    inserted_rows = 0
    inserted_rows_with_business_id = 0
    if available_columns:
        payloads = [
            build_insert_payload_variants(
                post,
                target=target_lookup.get(post.get("inputUrl") or ""),
                run_timestamp=run_timestamp,
                available_columns=available_columns,
            )[0]
            for post in posts
        ]
        if payloads:
            client.insert_rows(TABLE_NAME, payloads)
            inserted_rows = len(payloads)
            inserted_rows_with_business_id = sum(1 for payload in payloads if payload.get("business_id") is not None)
        return inserted_rows, insert_errors, inserted_rows_with_business_id

    for post in posts:
        target = target_lookup.get(post.get("inputUrl") or "")
        variants = build_insert_payload_variants(
            post,
            target=target,
            run_timestamp=run_timestamp,
            available_columns=available_columns,
        )
        last_error = ""
        for payload in variants:
            try:
                client.insert_rows(TABLE_NAME, [payload])
                inserted_rows += 1
                if payload.get("business_id") is not None:
                    inserted_rows_with_business_id += 1
                last_error = ""
                break
            except Exception as exc:  # pragma: no cover - exercised only in live mode
                last_error = str(exc)
        if last_error:
            insert_errors.append(
                {
                    "post_url": post.get("url") or "",
                    "inputUrl": post.get("inputUrl") or "",
                    "error": last_error,
                }
            )
    return inserted_rows, insert_errors, inserted_rows_with_business_id


def dedupe_posts(posts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for post in posts:
        key = (
            normalize_instagram_post_url(post.get("url") or ""),
            (post.get("local_post_date") or "").strip(),
        )
        if not key[0] or not key[1]:
            continue
        deduped[key] = post
    return list(deduped.values())


def collect_posts_in_window(
    raw_posts: Sequence[Dict[str, Any]],
    *,
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> List[Dict[str, Any]]:
    window_posts = [
        summarized
        for summarized in (
            summarize_filtered_post(item, timezone_name=timezone_name)
            for item in raw_posts
        )
        if summarized.get("local_post_date")
        and start_date.isoformat() <= summarized["local_post_date"] <= end_date.isoformat()
    ]
    return dedupe_posts(window_posts)


def fetch_posts_from_actor(
    args: argparse.Namespace,
    *,
    targets: Sequence[InstagramTarget],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    actor_runs: List[Dict[str, Any]] = []
    posts: List[Dict[str, Any]] = []
    direct_urls = [target.instagram_url for target in targets]
    for batch_index, batch_urls in enumerate(chunked(direct_urls, args.batch_size), start=1):
        run_info = run_actor(
            args.actor_id,
            batch_urls,
            results_limit=args.results_limit,
            only_posts_newer_than=resolve_only_posts_newer_than(args),
            actor_timeout_secs=args.actor_timeout_secs,
        )
        dataset_id = run_info.get("defaultDatasetId")
        if not dataset_id:
            raise RuntimeError("actor 运行缺少 defaultDatasetId")
        batch_posts = fetch_dataset_items(dataset_id)
        actor_runs.append(
            {
                "batch_index": batch_index,
                "batch_url_count": len(batch_urls),
                "actor_run_id": run_info.get("id"),
                "dataset_id": dataset_id,
                "fetched_items": len(batch_posts),
                "sample_input_urls": batch_urls[:5],
            }
        )
        posts.extend(batch_posts)
    return posts, actor_runs


def build_summary(
    *,
    status: str,
    report_path: Path,
    now: datetime,
    target_date: date,
    window_start_date: date,
    window_end_date: date,
    args: argparse.Namespace,
    instagram_urls_checked: int,
    posts_fetched: int,
    posts_passed_filter: int,
    rows_inserted: int,
    rows_skipped_duplicates: int,
    rows_with_business_id_backfilled: int,
    actor_items_total: int,
    dry_run: bool,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    summary = {
        "status": status,
        "report_path": str(report_path),
        "local_timezone": args.timezone,
        "current_local_timestamp": now.isoformat(),
        "target_local_date": target_date.isoformat(),
        "window_start_local_date": window_start_date.isoformat(),
        "window_end_local_date": window_end_date.isoformat(),
        "lookback_days": int(args.lookback_days or 1),
        "actor_only_posts_newer_than": resolve_only_posts_newer_than(args),
        "dry_run": dry_run,
        "instagram_urls_checked": instagram_urls_checked,
        "actor_items_total": actor_items_total,
        "posts_fetched": posts_fetched,
        "posts_passed_filter": posts_passed_filter,
        "rows_inserted": rows_inserted,
        "rows_skipped_duplicates": rows_skipped_duplicates,
        "rows_with_business_id_backfilled": rows_with_business_id_backfilled,
    }
    if error:
        summary["error"] = error
    return summary


def main() -> None:
    args = parse_args()
    now = datetime.now(ZoneInfo(args.timezone))
    target_date = resolve_target_date(args.local_date, args.timezone)
    window_start_date, window_end_date = resolve_target_date_window(target_date, args.lookback_days)
    report_path = resolve_report_path(now, args.lookback_days)

    instagram_urls_checked = 0
    actor_items_total = 0
    posts_fetched = 0
    posts_passed_filter = 0
    rows_inserted = 0
    rows_skipped_duplicates = 0
    rows_with_business_id_backfilled = 0
    actor_runs: List[Dict[str, Any]] = []
    insert_errors: List[Dict[str, Any]] = []
    eligible_targets: List[InstagramTarget] = []
    deduped_window_posts: List[Dict[str, Any]] = []
    filtered_posts: List[Dict[str, Any]] = []
    skipped_duplicate_posts: List[Dict[str, Any]] = []

    try:
        client = load_supabase_client()
        available_columns = detect_table_columns(client, TABLE_NAME)
        if args.fixture_posts_json:
            raw_posts = load_fixture_posts(args.fixture_posts_json)
            eligible_targets = enrich_targets_with_master_business_ids(
                client,
                build_fixture_targets(raw_posts, args.fixture_direct_urls_json),
            )
        else:
            eligible_targets = fetch_instagram_targets(client)

        if args.limit is not None:
            eligible_targets = eligible_targets[: args.limit]
        if not eligible_targets:
            raise RuntimeError("没有可处理的 Instagram URL")

        instagram_urls_checked = len(eligible_targets)
        target_lookup = build_target_lookup(eligible_targets)

        if args.fixture_posts_json:
            raw_posts = load_fixture_posts(args.fixture_posts_json)
        else:
            raw_posts, actor_runs = fetch_posts_from_actor(args, targets=eligible_targets)
        actor_items_total = len(raw_posts)

        deduped_window_posts = collect_posts_in_window(
            raw_posts,
            start_date=window_start_date,
            end_date=window_end_date,
            timezone_name=args.timezone,
        )
        posts_fetched = len(deduped_window_posts)

        filtered_posts = [post for post in deduped_window_posts if post.get("passed_promo_filter")]
        posts_passed_filter = len(filtered_posts)

        existing_keys: Set[Tuple[str, str]] = set()
        if client is not None:
            existing_keys = fetch_existing_post_keys(
                client,
                start_date=window_start_date,
                end_date=window_end_date,
                timezone_name=args.timezone,
                available_columns=available_columns,
            )

        posts_to_insert: List[Dict[str, Any]] = []
        for post in filtered_posts:
            key = (
                normalize_instagram_post_url(post.get("url") or ""),
                (post.get("local_post_date") or "").strip(),
            )
            if key in existing_keys:
                rows_skipped_duplicates += 1
                skipped_duplicate_posts.append(post)
                continue
            existing_keys.add(key)
            posts_to_insert.append(post)

        if client is not None:
            rows_inserted, insert_errors, rows_with_business_id_backfilled = insert_rows_with_fallback(
                client,
                posts=posts_to_insert,
                target_lookup=target_lookup,
                available_columns=available_columns,
                run_timestamp=now.astimezone(timezone.utc).isoformat(),
                dry_run=bool(args.dry_run),
            )

        summary = build_summary(
            status="completed",
            report_path=report_path,
            now=now,
            target_date=target_date,
            window_start_date=window_start_date,
            window_end_date=window_end_date,
            args=args,
            instagram_urls_checked=instagram_urls_checked,
            actor_items_total=actor_items_total,
            posts_fetched=posts_fetched,
            posts_passed_filter=posts_passed_filter,
            rows_inserted=rows_inserted,
            rows_skipped_duplicates=rows_skipped_duplicates,
            rows_with_business_id_backfilled=rows_with_business_id_backfilled,
            dry_run=bool(args.dry_run),
        )
        report = {
            **summary,
            "actor_runs": actor_runs,
            "eligible_targets_sample": [asdict(target) for target in eligible_targets[:10]],
            "filtered_posts_sample": filtered_posts[:10],
            "skipped_duplicate_posts_sample": skipped_duplicate_posts[:10],
            "insert_errors": insert_errors,
        }
    except Exception as exc:
        summary = build_summary(
            status="error",
            report_path=report_path,
            now=now,
            target_date=target_date,
            window_start_date=window_start_date,
            window_end_date=window_end_date,
            args=args,
            instagram_urls_checked=instagram_urls_checked,
            actor_items_total=actor_items_total,
            posts_fetched=posts_fetched,
            posts_passed_filter=posts_passed_filter,
            rows_inserted=rows_inserted,
            rows_skipped_duplicates=rows_skipped_duplicates,
            rows_with_business_id_backfilled=rows_with_business_id_backfilled,
            dry_run=bool(args.dry_run),
            error=str(exc),
        )
        report = {
            **summary,
            "actor_runs": actor_runs,
            "eligible_targets_sample": [asdict(target) for target in eligible_targets[:10]],
            "filtered_posts_sample": filtered_posts[:10],
            "skipped_duplicate_posts_sample": skipped_duplicate_posts[:10],
            "insert_errors": insert_errors,
        }

    write_json(report_path, report)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
