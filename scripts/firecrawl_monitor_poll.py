#!/usr/bin/env python3
"""
Poll Firecrawl monitors and trigger change-gated recrawls.

Only domains with meaningful monitor changes are re-crawled via Firecrawl crawl API
(with page cleaning) and synced into promo_website_staging.

Usage:
    python scripts/firecrawl_monitor_poll.py --dry-run
    python scripts/firecrawl_monitor_poll.py --limit 10
    python scripts/firecrawl_monitor_poll.py --monitor-id <id>
    python scripts/firecrawl_monitor_poll.py --since-check <check_id>

Environment:
    FIRECRAWL_API_KEY
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.promo_site_crawler import normalize_domain
from crawler.staging_recrawl import (
    DEFAULT_CRAWL_TIMEOUT_SECS,
    DEFAULT_MAX_CRAWL_PAGES,
    MonitorStateStore,
    load_supabase_client,
    recrawl_and_sync_domain,
)
from utils.firecrawl_client import get_firecrawl_client
from utils.logger import log
from utils.observability import init_observability
from utils.offer_extraction_llm import OpenAICompatibleClient, build_client_from_env

REPORT_PREFIX = "firecrawl_monitor_poll"
MONITOR_RESULTS_DIR = PROJECT_ROOT / "output" / "monitor_results"


def load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


try:
    # 用于在 get_monitor_check 中关闭 SDK 自动分页，由本模块显式按 skip 分页。
    from firecrawl.v2.types import PaginationConfig as _PaginationConfig
except Exception:  # pragma: no cover - SDK 版本差异时退化为依赖默认行为
    _PaginationConfig = None


def _obj_to_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return {"id": getattr(obj, "id", "?"), "name": getattr(obj, "name", "?")}


def _retry_firecrawl(label: str, func, *, retries: int = 5, delay: int = 2):
    for attempt in range(retries):
        try:
            return func()
        except Exception as exc:
            err_msg = str(exc)
            if "rate limit" in err_msg.lower() or "too many requests" in err_msg.lower() or "429" in err_msg:
                retry_match = re.search(r"retry after (\d+)s", err_msg)
                wait = int(retry_match.group(1)) + 2 if retry_match else delay * (2 ** attempt)
                log.warning("{label}: rate limited, waiting {wait}s", label=label, wait=wait)
                time.sleep(wait)
                continue
            if attempt == retries - 1:
                raise
            time.sleep(delay)
    raise RuntimeError(f"{label}: failed after {retries} retries")


def list_all_monitors(fc) -> List[Dict[str, Any]]:
    monitors: List[Dict[str, Any]] = []
    limit = 100
    offset = 0
    while True:
        result = _retry_firecrawl(
            "list_monitors",
            lambda: fc.list_monitors(limit=limit, offset=offset),
        )
        batch = result.data if hasattr(result, "data") else result
        if not isinstance(batch, list) or not batch:
            break
        monitors.extend(_obj_to_dict(item) for item in batch)
        if len(batch) < limit:
            break
        offset += limit
    return monitors


def list_monitor_checks(fc, monitor_id: str) -> List[Dict[str, Any]]:
    result = _retry_firecrawl(
        f"list_monitor_checks:{monitor_id}",
        lambda: fc.list_monitor_checks(monitor_id),
    )
    checks = result.data if hasattr(result, "data") else result
    if isinstance(checks, dict):
        checks = checks.get("data", [])
    if not isinstance(checks, list):
        return []
    return [_obj_to_dict(item) for item in checks]


def get_monitor_check_detail(
    fc,
    monitor_id: str,
    check_id: str,
    *,
    status: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
) -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {"limit": limit, "status": status, "skip": skip}
    if _PaginationConfig is not None:
        kwargs["pagination_config"] = _PaginationConfig(auto_paginate=False)
    result = _retry_firecrawl(
        f"get_monitor_check:{monitor_id}:{check_id}",
        lambda: fc.get_monitor_check(monitor_id, check_id, **kwargs),
    )
    data = result.data if hasattr(result, "data") else result
    return _obj_to_dict(data)


def infer_domain_from_monitor(monitor: Dict[str, Any], state_store: MonitorStateStore) -> str:
    monitor_id = monitor.get("id") or monitor.get("monitorId") or ""
    if monitor_id:
        state = state_store.get_state(monitor_id)
        if state and state.domain_name:
            return normalize_domain(state.domain_name)

    targets = monitor.get("targets") or []
    for target in targets:
        target_dict = _obj_to_dict(target)
        urls = target_dict.get("urls") or []
        for url in urls:
            domain = normalize_domain(url)
            if domain:
                return domain

    name = (monitor.get("name") or "").strip()
    if name:
        match = re.search(r"([a-z0-9][a-z0-9.-]+\.[a-z]{2,})", name.lower())
        if match:
            return normalize_domain(match.group(1))
    return ""


def check_has_changes(check: Dict[str, Any]) -> bool:
    summary = check.get("summary") or {}
    if not isinstance(summary, dict):
        return False
    return int(summary.get("changed") or 0) > 0 or int(summary.get("new") or 0) > 0


def page_is_meaningful(page: Dict[str, Any]) -> bool:
    status = (page.get("status") or "").lower()
    if status not in {"changed", "new"}:
        return False

    judgment = page.get("judgment")
    if judgment is None:
        return False

    judgment_dict = _obj_to_dict(judgment) if not isinstance(judgment, dict) else judgment
    meaningful = judgment_dict.get("meaningful")
    if meaningful is None:
        return False
    return bool(meaningful)


def extract_domains_from_check(
    fc,
    monitor_id: str,
    check_id: str,
    *,
    page_limit: int = 100,
) -> tuple[Set[str], int]:
    """返回 (meaningful 页面解析出的域名集合, meaningful 页面计数)。

    meaningful 计数用于区分"仅非 meaningful 变更"（计数为 0，不应重爬）
    与"有 meaningful 变更但域名解析不出"（计数>0，需保留重试）。
    """
    domains: Set[str] = set()
    meaningful_count = 0
    for status in ("changed", "new"):
        skip = 0
        while True:
            detail = get_monitor_check_detail(
                fc, monitor_id, check_id, status=status, limit=page_limit, skip=skip
            )
            pages = detail.get("pages") or []
            for page in pages:
                page_dict = _obj_to_dict(page)
                if not page_is_meaningful(page_dict):
                    continue
                meaningful_count += 1
                url = page_dict.get("url") or ""
                domain = normalize_domain(url)
                if domain:
                    domains.add(domain)
            # 拉满一页说明可能还有后续页，按 skip 继续；不足一页则结束。
            if len(pages) < page_limit:
                break
            skip += page_limit
    return domains, meaningful_count


def fetch_meaningful_pages(
    fc,
    monitor_id: str,
    check_id: str,
    *,
    page_limit: int = 100,
) -> Tuple[List[Dict[str, Any]], int]:
    """Fetch all meaningful changed/new pages with full diff and judgment data.

    Unlike extract_domains_from_check (which only returns domain names),
    this function preserves the complete page dict—including diff.json,
    diff.text, and judgment.meaningfulChanges—needed by change_driven_extractor.

    Returns (meaningful_pages, meaningful_count).
    """
    pages: List[Dict[str, Any]] = []
    for status in ("changed", "new"):
        skip = 0
        while True:
            detail = get_monitor_check_detail(
                fc, monitor_id, check_id, status=status, limit=page_limit, skip=skip
            )
            page_batch = detail.get("pages") or []
            for page in page_batch:
                page_dict = _obj_to_dict(page)
                if page_is_meaningful(page_dict):
                    pages.append(page_dict)
            if len(page_batch) < page_limit:
                break
            skip += page_limit
    return pages, len(pages)


def sort_checks_newest_first(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def sort_key(check: Dict[str, Any]):
        created = check.get("created_at") or check.get("createdAt") or ""
        return created

    return sorted(checks, key=sort_key, reverse=True)


def select_checks_to_process(
    checks: List[Dict[str, Any]],
    *,
    last_check_id: Optional[str],
    since_check: Optional[str] = None,
    baseline_only: bool = False,
    force_latest: bool = False,
) -> List[Dict[str, Any]]:
    ordered = sort_checks_newest_first(checks)
    if not ordered:
        return []

    if force_latest:
        return [ordered[0]]

    if since_check:
        try:
            start_idx = next(i for i, check in enumerate(ordered) if check.get("id") == since_check)
        except StopIteration:
            return ordered
        return list(reversed(ordered[:start_idx]))

    if baseline_only or not last_check_id:
        return []

    try:
        last_idx = next(i for i, check in enumerate(ordered) if check.get("id") == last_check_id)
    except StopIteration:
        # Unknown cursor: treat latest check as baseline only.
        return []

    newer_checks = ordered[:last_idx]
    return list(reversed(newer_checks))


def recrawl_domains(
    domains: Iterable[str],
    *,
    client,
    max_crawl_pages: int,
    crawl_timeout_secs: int,
) -> Dict[str, Any]:
    results: Dict[str, Any] = {}
    for domain in domains:
        try:
            results[domain] = recrawl_and_sync_domain(
                domain,
                client=client,
                dry_run=False,
                max_crawl_pages=max_crawl_pages,
                crawl_timeout_secs=crawl_timeout_secs,
            )
        except Exception as exc:
            results[domain] = {"action": "error", "error": str(exc)}
    return results


def resolve_report_path(prefix: str = REPORT_PREFIX) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    MONITOR_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return MONITOR_RESULTS_DIR / f"{prefix}_{timestamp}.json"


def process_monitor(
    fc,
    monitor: Dict[str, Any],
    state_store: MonitorStateStore,
    supabase_client,
    *,
    dry_run: bool,
    max_crawl_pages: int,
    crawl_timeout_secs: int,
    since_check: Optional[str],
    force_reprocess_latest: bool,
    llm_client: Optional[OpenAICompatibleClient] = None,
    skip_apify_on_success: bool = False,
    min_confidence: str = "low",
    include_change_events: bool = False,
) -> Dict[str, Any]:
    monitor_id = monitor.get("id") or monitor.get("monitorId") or ""
    monitor_name = monitor.get("name") or monitor_id
    fallback_domain = infer_domain_from_monitor(monitor, state_store)

    checks = list_monitor_checks(fc, monitor_id)
    state = state_store.get_state(monitor_id)
    last_check_id = state.last_check_id if state else None
    domain_name = (state.domain_name if state and state.domain_name else fallback_domain) or fallback_domain

    if domain_name:
        state_store.upsert_mapping(monitor_id, domain_name)

    ordered = sort_checks_newest_first(checks)
    if not ordered:
        return {
            "monitor_id": monitor_id,
            "name": monitor_name,
            "domain_name": domain_name,
            "status": "no_checks",
            "checks_seen": 0,
        }

    baseline_only = not last_check_id and not since_check and not force_reprocess_latest
    checks_to_process = select_checks_to_process(
        checks,
        last_check_id=last_check_id,
        since_check=since_check,
        baseline_only=baseline_only,
        force_latest=force_reprocess_latest,
    )

    if baseline_only:
        latest = ordered[0]
        latest_id = latest.get("id")
        if not dry_run and latest_id:
            state_store.save_state(
                monitor_id=monitor_id,
                domain_name=domain_name or fallback_domain or "unknown",
                last_check_id=latest_id,
            )
        return {
            "monitor_id": monitor_id,
            "name": monitor_name,
            "domain_name": domain_name,
            "status": "baseline_initialized",
            "baseline_check_id": latest_id,
            "checks_seen": len(checks),
        }

    monitor_report: Dict[str, Any] = {
        "monitor_id": monitor_id,
        "name": monitor_name,
        "domain_name": domain_name,
        "status": "processed",
        "checks_seen": len(checks),
        "checks_processed": [],
        "recrawls": [],
    }

    for check in checks_to_process:
        check_id = check.get("id")
        if not check_id:
            continue

        check_status = (check.get("status") or "").lower()
        if check_status and check_status not in {"completed", "complete", "success", "succeeded"}:
            monitor_report["checks_processed"].append(
                {
                    "check_id": check_id,
                    "status": check_status,
                    "action": "skipped_not_completed",
                }
            )
            if not dry_run:
                state_store.save_state(
                    monitor_id=monitor_id,
                    domain_name=domain_name or fallback_domain or "unknown",
                    last_check_id=check_id,
                    last_change_at=state.last_change_at if state else None,
                    last_processed_at=state.last_processed_at if state else None,
                )
            continue

        trigger_recrawl = check_has_changes(check)
        domains_to_recrawl: Set[str] = set()
        meaningful_count = 0
        # Full page objects (with diff/judgment) are only fetched when an LLM
        # client is available; otherwise the cheaper domain-only path is used.
        meaningful_pages: List[Dict[str, Any]] = []
        if trigger_recrawl:
            if llm_client is not None:
                meaningful_pages, meaningful_count = fetch_meaningful_pages(
                    fc, monitor_id, check_id
                )
                for page in meaningful_pages:
                    d = normalize_domain(page.get("url") or "")
                    if d:
                        domains_to_recrawl.add(d)
            else:
                domains_to_recrawl, meaningful_count = extract_domains_from_check(
                    fc, monitor_id, check_id
                )
            # 仅当确实存在 meaningful 页面时才回退到 monitor 绑定域名；
            # 若 summary 报告了变更但没有任何 meaningful 页面，则不重爬（M1 修复）。
            if not domains_to_recrawl and meaningful_count > 0 and domain_name:
                domains_to_recrawl.add(normalize_domain(domain_name))

        check_entry: Dict[str, Any] = {
            "check_id": check_id,
            "summary": check.get("summary"),
            "trigger_recrawl": trigger_recrawl and bool(domains_to_recrawl),
            "domains": sorted(domains_to_recrawl),
        }

        if check_entry["trigger_recrawl"]:
            now_iso = datetime.now(timezone.utc).isoformat()
            recrawl_had_error = False

            # --- Change-driven extraction (fast path) ----------------------------
            # When an LLM client is supplied, attempt to extract offers directly
            # from the Firecrawl diff data, avoiding a full Apify recrawl.
            # If every changed page carried usable diff data AND skip_apify_on_success
            # is set, the Apify recrawl is skipped entirely for this check.
            change_driven_result: Optional[Dict[str, Any]] = None
            skip_apify_this_check = False

            if llm_client is not None and meaningful_pages:
                from utils.change_driven_extractor import extract_and_upsert_check_pages
                try:
                    change_driven_result = extract_and_upsert_check_pages(
                        meaningful_pages,
                        llm_client,
                        supabase_client,
                        domain_name or "",
                        dry_run=dry_run,
                        min_confidence=min_confidence,
                        include_change_events=include_change_events,
                    )
                    check_entry["change_driven"] = change_driven_result
                    if (
                        skip_apify_on_success
                        and not change_driven_result["needs_apify_fallback"]
                    ):
                        skip_apify_this_check = True
                        log.info(
                            "change_driven: all pages covered for check {cid}, skipping Apify",
                            cid=check_id,
                        )
                except Exception as exc:
                    log.error(
                        "change_driven: pipeline failed for check {cid}: {error}",
                        cid=check_id,
                        error=exc,
                    )
                    check_entry["change_driven_error"] = str(exc)
            # --- End change-driven extraction ------------------------------------

            if skip_apify_this_check:
                monitor_report["recrawls"].append(
                    {
                        "domain": domain_name,
                        "action": "skipped_change_driven",
                        "change_driven": change_driven_result,
                    }
                )
            elif dry_run:
                for domain in sorted(domains_to_recrawl):
                    monitor_report["recrawls"].append(
                        {"domain": domain, "dry_run": True, "action": "would_recrawl"}
                    )
            else:
                recrawl_results = recrawl_domains(
                    domains_to_recrawl,
                    client=supabase_client,
                    max_crawl_pages=max_crawl_pages,
                    crawl_timeout_secs=crawl_timeout_secs,
                )
                for domain, result in recrawl_results.items():
                    entry = {"domain": domain, "dry_run": False, **result}
                    if result.get("action") == "error":
                        monitor_report["status"] = "partial_error"
                        recrawl_had_error = True
                    monitor_report["recrawls"].append(entry)

            # 仅当所有域名重爬都成功时才推进游标，避免失败的 meaningful 变更被永久跳过。
            if not dry_run and not recrawl_had_error:
                state_store.save_state(
                    monitor_id=monitor_id,
                    domain_name=domain_name or fallback_domain or "unknown",
                    last_check_id=check_id,
                    last_change_at=now_iso,
                    last_processed_at=now_iso,
                )
            elif not dry_run and recrawl_had_error:
                check_entry["cursor_advanced"] = False
        elif trigger_recrawl and meaningful_count > 0:
            # 有 meaningful 页面但既没解析出域名、domain_name 也为空：
            # 这与"真正无变更"不同，不能误判并推进游标，保留旧游标以便后续重试。
            check_entry["action"] = "unresolved_domain"
            if not dry_run:
                check_entry["cursor_advanced"] = False
        else:
            # 无任何 meaningful 页面（summary 可能只报告了非 meaningful 变更）
            # 或确实无变更：不重爬，推进游标。
            check_entry["action"] = "no_meaningful_change"
            if not dry_run:
                state_store.save_state(
                    monitor_id=monitor_id,
                    domain_name=domain_name or fallback_domain or "unknown",
                    last_check_id=check_id,
                    last_change_at=state.last_change_at if state else None,
                    last_processed_at=state.last_processed_at if state else None,
                )

        monitor_report["checks_processed"].append(check_entry)
        state = state_store.get_state(monitor_id)

        # check 按时间从旧到新处理：一旦某个 check 重爬失败就停止，
        # 不再处理更新的 check，以免游标越过失败的 check 导致其被永久跳过。
        if check_entry.get("cursor_advanced") is False:
            break

    if not monitor_report["checks_processed"]:
        latest = ordered[0]
        latest_id = latest.get("id")
        if not dry_run and latest_id:
            state_store.save_state(
                monitor_id=monitor_id,
                domain_name=domain_name or fallback_domain or "unknown",
                last_check_id=latest_id,
                last_change_at=state.last_change_at if state else None,
                last_processed_at=state.last_processed_at if state else None,
            )
        monitor_report["status"] = "up_to_date"

    return monitor_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll Firecrawl monitors and trigger change-gated recrawls")
    parser.add_argument("--dry-run", action="store_true", help="Detect changes but do not recrawl or write state")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N monitors")
    parser.add_argument("--monitor-id", default=None, help="Process a single monitor id")
    parser.add_argument("--since-check", default=None, help="Reprocess checks newer than this check id")
    parser.add_argument(
        "--force-latest",
        action="store_true",
        help="Reprocess the latest check (even if already recorded in local state)",
    )
    parser.add_argument("--max-crawl-pages", type=int, default=DEFAULT_MAX_CRAWL_PAGES, help="Max pages per Firecrawl crawl")
    parser.add_argument(
        "--crawl-timeout-secs",
        type=int,
        default=DEFAULT_CRAWL_TIMEOUT_SECS,
        help="Firecrawl crawl timeout in seconds",
    )
    # Change-driven extraction options
    parser.add_argument(
        "--llm-api-url",
        default=None,
        help="OpenAI-compatible chat completions URL for change-driven extraction "
             "(env: LLM_API_URL)",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        help="Model name for change-driven extraction (env: LLM_MODEL)",
    )
    parser.add_argument(
        "--llm-api-key-env",
        default="LLM_API_KEY",
        help="Env var holding the LLM API key (default: LLM_API_KEY)",
    )
    parser.add_argument(
        "--skip-apify-on-success",
        action="store_true",
        help="Skip Apify recrawl when change-driven extraction covers all changed pages",
    )
    parser.add_argument(
        "--min-confidence",
        default="low",
        choices=["low", "medium", "high"],
        help="Skip LLM extraction for pages below this confidence (default: low = keep all)",
    )
    parser.add_argument(
        "--include-change-events",
        action="store_true",
        help="Include dry-run promo_offer_change_events payloads in the report",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_observability()
    load_env()

    fc = get_firecrawl_client()
    supabase_client = load_supabase_client(PROJECT_ROOT)
    state_store = MonitorStateStore(supabase_client)

    llm_client = build_client_from_env(
        api_url=args.llm_api_url,
        model=args.llm_model,
        api_key_env=args.llm_api_key_env,
    )
    if llm_client:
        log.info(
            "Change-driven extraction enabled: model={model}, skip_apify={skip}",
            model=llm_client.model,
            skip=bool(args.skip_apify_on_success),
        )
    else:
        log.info("Change-driven extraction disabled (LLM_API_URL/LLM_MODEL/LLM_API_KEY not set)")

    monitors = list_all_monitors(fc)
    if args.monitor_id:
        monitors = [m for m in monitors if (m.get("id") or m.get("monitorId")) == args.monitor_id]
    if args.limit is not None:
        monitors = monitors[: args.limit]

    if not monitors:
        print("No monitors matched the selection.")
        return

    reports: List[Dict[str, Any]] = []
    triggered = 0
    errors = 0

    for index, monitor in enumerate(monitors, start=1):
        monitor_id = monitor.get("id") or monitor.get("monitorId") or "?"
        monitor_name = monitor.get("name") or monitor_id
        log.info("[{index}/{total}] Processing monitor {name}", index=index, total=len(monitors), name=monitor_name)
        try:
            report = process_monitor(
                fc,
                monitor,
                state_store,
                supabase_client,
                dry_run=bool(args.dry_run),
                max_crawl_pages=max(1, args.max_crawl_pages),
                crawl_timeout_secs=max(60, args.crawl_timeout_secs),
                since_check=args.since_check,
                force_reprocess_latest=bool(args.force_latest),
                llm_client=llm_client,
                skip_apify_on_success=bool(args.skip_apify_on_success),
                min_confidence=args.min_confidence,
                include_change_events=bool(args.include_change_events),
            )
            reports.append(report)
            if any(item.get("trigger_recrawl") for item in report.get("checks_processed", [])):
                triggered += 1
        except Exception as exc:
            errors += 1
            reports.append(
                {
                    "monitor_id": monitor_id,
                    "name": monitor_name,
                    "status": "error",
                    "error": str(exc),
                }
            )
            log.error("Monitor processing failed for {monitor_id}: {error}", monitor_id=monitor_id, error=exc)

    summary = {
        "status": "completed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(args.dry_run),
        "monitors_seen": len(monitors),
        "monitors_with_triggered_recrawl": triggered,
        "errors": errors,
    }
    final_report = {"summary": summary, "monitors": reports}
    report_path = resolve_report_path()
    report_path.write_text(json.dumps(final_report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({**summary, "report_path": str(report_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
