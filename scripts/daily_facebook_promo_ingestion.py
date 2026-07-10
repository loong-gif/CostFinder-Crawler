#!/usr/bin/env python3
"""
Run the daily Facebook promo ingestion job.
"""
from __future__ import annotations

import argparse
import concurrent.futures
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
from utils.supabase_rest import SupabaseRestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_DIR
from crawler.promo_site_crawler import is_filtered_process_flag
from utils.facebook_promo_filter import (
    normalize_facebook_post_url,
    normalize_facebook_profile_url,
    resolve_post_local_date,
    summarize_filtered_post,
)

TABLE_NAME = "promo_social_staging"
DEFAULT_ACTOR_ID = os.getenv("APIFY_FACEBOOK_ACTOR_ID") or os.getenv("FACEBOOK_ACTOR_ID") or "apify/facebook-posts-scraper"
DEFAULT_TIMEZONE = os.getenv("TZ") or "Asia/Shanghai"
DEFAULT_RESULTS_LIMIT = 20
DEFAULT_BATCH_SIZE = 10
DEFAULT_ACTOR_TIMEOUT_SECS = 1800
DEFAULT_ONLY_POSTS_NEWER_THAN = "7 days"
TIMESTAMP_COLUMN_CANDIDATES = ["local_post_date", "published_at", "posted_at", "timestamp", "crawl_timestamp", "created_at"]
POST_URL_COLUMN_CANDIDATES = ["post_url", "url", "postUrl", "source_url"]


@dataclass(frozen=True)
class FacebookTarget:
    master_id: Optional[int]
    business_id: Optional[int]
    name: str
    facebook_url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按日抓取 Facebook 促销内容并写入 promo_social_staging")
    parser.add_argument("--actor-id", default=DEFAULT_ACTOR_ID, help="Apify Facebook actor ID")
    parser.add_argument("--results-limit", type=int, default=DEFAULT_RESULTS_LIMIT, help="每个页面最多拉取的帖子数")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="每次调用 actor 的页面数")
    parser.add_argument("--batch-concurrency", type=int, default=1, help="并行调用 actor 的批次数")
    parser.add_argument("--actor-timeout-secs", type=int, default=DEFAULT_ACTOR_TIMEOUT_SECS, help="单次 actor 调用超时")
    parser.add_argument("--only-posts-newer-than", default=DEFAULT_ONLY_POSTS_NEWER_THAN, help="传给 actor 的只抓取最近帖子窗口")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="用于判定当前本地日期的时区")
    parser.add_argument("--local-date", default=None, help="覆盖当前本地日期，格式 YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 个 Facebook URL")
    parser.add_argument("--dry-run", action="store_true", help="只生成 artifact，不写入 Supabase")
    parser.add_argument("--fixture-posts-json", default=None, help="离线调试：直接读取本地 actor 输出 JSON")
    parser.add_argument("--fixture-start-urls-json", default=None, help="离线调试：读取本地 actor 输入 JSON，提取 startUrls")
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
        batch = client.fetch_rows(table, select, filters=filters, limit=page_size, offset=offset, order=order)
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


def resolve_report_path(now: datetime) -> Path:
    timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
    return OUTPUT_DIR / f"facebook_promo_daily_ingestion_{timestamp}.json"


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
    start_urls: Sequence[str],
    *,
    results_limit: int,
    only_posts_newer_than: str,
    actor_timeout_secs: int,
) -> Dict[str, Any]:
    actor_input = {
        "startUrls": [{"url": url} for url in start_urls],
        "resultsLimit": results_limit,
        "captionText": False,
        "onlyPostsNewerThan": only_posts_newer_than,
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


def load_fixture_start_urls(path_str: str) -> List[str]:
    path = Path(path_str).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("fixture start URLs JSON 必须是对象")
    raw_urls = payload.get("startUrls") or []
    if not isinstance(raw_urls, list):
        raise RuntimeError("fixture start URLs JSON 的 startUrls 必须是数组")
    urls: List[str] = []
    for value in raw_urls:
        if isinstance(value, dict):
            normalized = normalize_facebook_profile_url(value.get("url") or "")
        else:
            normalized = normalize_facebook_profile_url(str(value))
        if normalized:
            urls.append(normalized)
    return urls


def build_fixture_targets(posts: Sequence[Dict[str, Any]], fixture_start_urls_json: Optional[str]) -> List[FacebookTarget]:
    urls: List[str] = []
    if fixture_start_urls_json:
        urls.extend(load_fixture_start_urls(fixture_start_urls_json))
    else:
        for item in posts:
            normalized = normalize_facebook_profile_url(item.get("inputUrl") or "")
            if normalized:
                urls.append(normalized)

    deduped: List[FacebookTarget] = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(FacebookTarget(master_id=None, business_id=None, name="", facebook_url=url))
    return deduped


def fetch_facebook_targets(client: SupabaseRestClient) -> List[FacebookTarget]:
    rows = fetch_all_rows(
        client,
        "master_business_info",
        "id,business_id,name,facebook_url,process_flag",
        filters={"facebook_url": "not.is.null"},
        order="id.asc",
    )
    targets: List[FacebookTarget] = []
    seen_urls = set()
    for row in rows:
        if is_filtered_process_flag(row.get("process_flag")):
            continue
        normalized_url = normalize_facebook_profile_url(row.get("facebook_url") or "")
        if not normalized_url or normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        targets.append(
            FacebookTarget(
                master_id=row.get("id"),
                business_id=row.get("business_id"),
                name=(row.get("name") or "").strip(),
                facebook_url=normalized_url,
            )
        )
    return targets


def enrich_targets_with_master_business_ids(
    client: SupabaseRestClient,
    targets: Sequence[FacebookTarget],
) -> List[FacebookTarget]:
    master_rows = fetch_all_rows(
        client,
        "master_business_info",
        "id,business_id,name,facebook_url,process_flag",
        filters={"facebook_url": "not.is.null"},
        order="id.asc",
    )
    master_by_url: Dict[str, Dict[str, Any]] = {}
    for row in master_rows:
        if is_filtered_process_flag(row.get("process_flag")):
            continue
        normalized_url = normalize_facebook_profile_url(row.get("facebook_url") or "")
        if not normalized_url or normalized_url in master_by_url:
            continue
        master_by_url[normalized_url] = row

    enriched: List[FacebookTarget] = []
    for target in targets:
        master_row = master_by_url.get(target.facebook_url)
        if master_row is None:
            enriched.append(target)
            continue
        enriched.append(
            FacebookTarget(
                master_id=master_row.get("id"),
                business_id=master_row.get("business_id"),
                name=(master_row.get("name") or target.name or "").strip(),
                facebook_url=target.facebook_url,
            )
        )
    return enriched


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
        properties = spec.get("definitions", {}).get(table, {}).get("properties", {})
        return set(properties.keys())
    return set(rows[0].keys())


def build_target_lookup(targets: Sequence[FacebookTarget]) -> Dict[str, FacebookTarget]:
    return {target.facebook_url: target for target in targets}


def local_day_bounds_utc(target_date: date, timezone_name: str) -> Tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    local_start = datetime.combine(target_date, time.min, tzinfo=zone)
    local_end = local_start + timedelta(days=1)
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
        raw_value = stringify_timestamp(row.get(key))
        local_value = resolve_post_local_date(raw_value, None, timezone_name=timezone_name)
        if local_value:
            return local_value.isoformat()
    return ""


def resolve_existing_row_post_url(row: Dict[str, Any]) -> str:
    for key in POST_URL_COLUMN_CANDIDATES:
        normalized = normalize_facebook_post_url(row.get(key) or "")
        if normalized:
            return normalized
    return ""


def fetch_existing_post_keys(
    client: SupabaseRestClient,
    *,
    target_date: date,
    timezone_name: str,
    available_columns: Set[str],
) -> Set[Tuple[str, str]]:
    if not available_columns:
        return set()

    rows: List[Dict[str, Any]] = []
    if "local_post_date" in available_columns:
        rows = fetch_all_rows(client, TABLE_NAME, "*", filters={"local_post_date": f"eq.{target_date.isoformat()}"})
    else:
        timestamp_column = next((name for name in TIMESTAMP_COLUMN_CANDIDATES if name in available_columns and name != "local_post_date"), None)
        if timestamp_column:
            utc_start, utc_end = local_day_bounds_utc(target_date, timezone_name)
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


def build_base_insert_payload(post: Dict[str, Any], target: Optional[FacebookTarget], run_timestamp: str) -> Dict[str, Any]:
    published_at = stringify_timestamp(post.get("time"))
    input_url = normalize_facebook_profile_url(post.get("inputUrl") or "")
    post_url = normalize_facebook_post_url(post.get("url") or "")
    source_metadata = {
        "inputUrl": input_url or (post.get("inputUrl") or ""),
        "url": post_url or (post.get("url") or ""),
        "pageName": post.get("pageName"),
        "facebookId": post.get("facebookId"),
        "postId": post.get("postId"),
    }
    return {
        "business_id": target.business_id if target else None,
        "name": target.name if target else None,
        "platform": "facebook",
        "source_platform": "facebook",
        "facebook_url": input_url or (post.get("inputUrl") or ""),
        "input_url": input_url or (post.get("inputUrl") or ""),
        "inputUrl": input_url or (post.get("inputUrl") or ""),
        "post_url": post_url or (post.get("url") or ""),
        "postUrl": post_url or (post.get("url") or ""),
        "url": post_url or (post.get("url") or ""),
        "caption": (post.get("text") or "").strip(),
        "promo_text_raw": (post.get("text") or "").strip(),
        "published_at": published_at,
        "posted_at": published_at,
        "timestamp": (post.get("local_post_date") or published_at),
        "crawl_timestamp": run_timestamp,
        "page_name": post.get("pageName"),
        "pageName": post.get("pageName"),
        "facebook_id": post.get("facebookId"),
        "post_id": post.get("postId"),
        "likes_count": post.get("likes"),
        "comments_count": post.get("comments"),
        "shares_count": post.get("shares"),
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
    target: Optional[FacebookTarget],
    run_timestamp: str,
    available_columns: Set[str],
) -> List[Dict[str, Any]]:
    base_payload = build_base_insert_payload(post, target, run_timestamp)
    if available_columns:
        return [{key: value for key, value in base_payload.items() if key in available_columns}]

    snake_case = {
        key: value
        for key, value in base_payload.items()
        if key
        in {
            "business_id",
            "name",
            "platform",
            "facebook_url",
            "input_url",
            "post_url",
            "caption",
            "promo_text_raw",
            "published_at",
            "crawl_timestamp",
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
        "platform": "facebook",
        "inputUrl": base_payload["inputUrl"],
        "postUrl": base_payload["postUrl"],
        "url": base_payload["url"],
        "caption": base_payload["caption"],
        "published_at": base_payload["published_at"],
        "crawl_timestamp": base_payload["crawl_timestamp"],
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
    target_lookup: Dict[str, FacebookTarget],
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
        variants = build_insert_payload_variants(post, target=target, run_timestamp=run_timestamp, available_columns=available_columns)
        last_error = ""
        for payload in variants:
            try:
                client.insert_rows(TABLE_NAME, [payload])
                inserted_rows += 1
                if payload.get("business_id") is not None:
                    inserted_rows_with_business_id += 1
                last_error = ""
                break
            except Exception as exc:
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


def dedupe_posts(posts: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    skipped_duplicates: List[Dict[str, Any]] = []
    for post in posts:
        key = (
            normalize_facebook_post_url(post.get("url") or ""),
            (post.get("local_post_date") or "").strip(),
        )
        if not key[0] or not key[1]:
            continue
        if key in deduped:
            skipped_duplicates.append(post)
        deduped[key] = post
    return list(deduped.values()), skipped_duplicates


def fetch_posts_from_actor(
    args: argparse.Namespace,
    *,
    targets: Sequence[FacebookTarget],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    start_urls = [target.facebook_url for target in targets]
    batches = list(enumerate(chunked(start_urls, args.batch_size), start=1))
    if not batches:
        return [], []

    def run_single_batch(batch_index: int, batch_urls: Sequence[str]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        run_info = run_actor(
            args.actor_id,
            batch_urls,
            results_limit=args.results_limit,
            only_posts_newer_than=args.only_posts_newer_than,
            actor_timeout_secs=args.actor_timeout_secs,
        )
        dataset_id = run_info.get("defaultDatasetId")
        if not dataset_id:
            raise RuntimeError("actor 运行缺少 defaultDatasetId")
        batch_posts = fetch_dataset_items(dataset_id)
        actor_run = {
            "batch_index": batch_index,
            "batch_url_count": len(batch_urls),
            "actor_run_id": run_info.get("id"),
            "dataset_id": dataset_id,
            "fetched_items": len(batch_posts),
            "sample_input_urls": list(batch_urls[:5]),
        }
        return batch_posts, actor_run

    posts: List[Dict[str, Any]] = []
    actor_runs: List[Dict[str, Any]] = []
    max_workers = max(1, args.batch_concurrency)
    if max_workers == 1:
        for batch_index, batch_urls in batches:
            batch_posts, actor_run = run_single_batch(batch_index, batch_urls)
            actor_runs.append(actor_run)
            posts.extend(batch_posts)
        return posts, actor_runs

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_batch = {
            executor.submit(run_single_batch, batch_index, batch_urls): (batch_index, batch_urls)
            for batch_index, batch_urls in batches
        }
        for future in concurrent.futures.as_completed(future_to_batch):
            batch_posts, actor_run = future.result()
            actor_runs.append(actor_run)
            posts.extend(batch_posts)

    actor_runs.sort(key=lambda item: item.get("batch_index") or 0)
    return posts, actor_runs


def build_summary(
    *,
    status: str,
    report_path: Path,
    now: datetime,
    target_date: date,
    args: argparse.Namespace,
    facebook_urls_checked: int,
    posts_fetched: int,
    posts_matching_current_local_date: int,
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
        "dry_run": dry_run,
        "facebook_urls_checked": facebook_urls_checked,
        "actor_items_total": actor_items_total,
        "posts_fetched": posts_fetched,
        "posts_matching_current_local_date": posts_matching_current_local_date,
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
    report_path = resolve_report_path(now)

    facebook_urls_checked = 0
    actor_items_total = 0
    posts_fetched = 0
    posts_matching_current_local_date = 0
    posts_passed_filter = 0
    rows_inserted = 0
    rows_skipped_duplicates = 0
    rows_with_business_id_backfilled = 0
    actor_runs: List[Dict[str, Any]] = []
    insert_errors: List[Dict[str, Any]] = []
    eligible_targets: List[FacebookTarget] = []
    deduped_today_posts: List[Dict[str, Any]] = []
    filtered_posts: List[Dict[str, Any]] = []
    skipped_duplicate_posts: List[Dict[str, Any]] = []

    try:
        client = load_supabase_client()
        available_columns = detect_table_columns(client, TABLE_NAME)
        if args.fixture_posts_json:
            raw_posts = load_fixture_posts(args.fixture_posts_json)
            eligible_targets = enrich_targets_with_master_business_ids(
                client,
                build_fixture_targets(raw_posts, args.fixture_start_urls_json),
            )
        else:
            eligible_targets = fetch_facebook_targets(client)

        if args.limit is not None:
            eligible_targets = eligible_targets[: args.limit]
        if not eligible_targets:
            raise RuntimeError("没有可处理的 Facebook URL")

        facebook_urls_checked = len(eligible_targets)
        target_lookup = build_target_lookup(eligible_targets)

        if args.fixture_posts_json:
            raw_posts = load_fixture_posts(args.fixture_posts_json)
        else:
            raw_posts, actor_runs = fetch_posts_from_actor(args, targets=eligible_targets)
        actor_items_total = len(raw_posts)

        current_date_posts = [
            summarized
            for summarized in (summarize_filtered_post(item, timezone_name=args.timezone) for item in raw_posts)
            if summarized.get("local_post_date") == target_date.isoformat()
        ]
        posts_matching_current_local_date = len(current_date_posts)
        deduped_today_posts, duplicate_posts_in_run = dedupe_posts(current_date_posts)
        rows_skipped_duplicates += len(duplicate_posts_in_run)
        skipped_duplicate_posts.extend(duplicate_posts_in_run)
        posts_fetched = len(deduped_today_posts)

        filtered_posts = [post for post in deduped_today_posts if post.get("passed_promo_filter")]
        posts_passed_filter = len(filtered_posts)

        existing_keys: Set[Tuple[str, str]] = set()
        existing_keys = fetch_existing_post_keys(
            client,
            target_date=target_date,
            timezone_name=args.timezone,
            available_columns=available_columns,
        )

        posts_to_insert: List[Dict[str, Any]] = []
        for post in filtered_posts:
            key = (
                normalize_facebook_post_url(post.get("url") or ""),
                (post.get("local_post_date") or "").strip(),
            )
            if key in existing_keys:
                rows_skipped_duplicates += 1
                skipped_duplicate_posts.append(post)
                continue
            existing_keys.add(key)
            posts_to_insert.append(post)

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
            args=args,
            facebook_urls_checked=facebook_urls_checked,
            actor_items_total=actor_items_total,
            posts_fetched=posts_fetched,
            posts_matching_current_local_date=posts_matching_current_local_date,
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
            args=args,
            facebook_urls_checked=facebook_urls_checked,
            actor_items_total=actor_items_total,
            posts_fetched=posts_fetched,
            posts_matching_current_local_date=posts_matching_current_local_date,
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
