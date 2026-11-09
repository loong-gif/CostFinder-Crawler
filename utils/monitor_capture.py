"""监控抓取阶段：选出本轮要抓的页面，逐个监控组抓取，并把结果登记到轮询档期。"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from utils.monitor_event_queue import retry_failed_scrape_urls
from utils.monitor_frequency import due_urls, load_schedule, record_reports
from utils.monitor_price_store import atomic_json, now
from utils.monitor_queue import config_kinds, config_urls


def capture_urls(root: Path, config: dict, quality_urls: set[str], *, retry_only: bool,
                 capture_all: bool, started: datetime) -> set[str] | None:
    """None 表示抓取配置里的全部页面。"""
    if retry_only:
        return retry_failed_scrape_urls(root, config)
    if capture_all:
        return None
    # 每天运行：只抓轮询到期的页面，加上到期活动与数据质量复查要求的页面
    configured = set(config_urls(config))
    schedule = load_schedule(root, configured, started, config_kinds(config))
    return due_urls(schedule, configured, started) | quality_urls


def capture(root: Path, config: dict, names: list[str], selected_urls: set[str] | None, *, limit: int | None,
            catchup: bool, started: datetime, run_monitor: Callable) -> list[dict]:
    reports = []
    browser_cache: dict = {}
    for name in names:
        target_urls = {url for target in config["monitors"][name].get("targets", []) for url in target.get("urls", [])}
        filtered = {url for url in selected_urls if url in target_urls} if selected_urls is not None else None
        if filtered is not None and not filtered:
            continue
        reports.append(run_monitor(config, name, api_url=os.getenv("FIRECRAWL_SELF_HOSTED_API_URL", "http://127.0.0.1:3002"),
            api_key=os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY", ""), state_dir=root,
            timeout=float(os.getenv("FIRECRAWL_HTTP_TIMEOUT_SECS", "120")), limit=limit,
            workers=int(os.getenv("FIRECRAWL_MONITOR_WORKERS", "3")), send_notifications=False,
            catchup=catchup, urls_filter=filtered,
            scan_images=config["monitors"][name].get("scan_images", True), browser_cache=browser_cache))
        atomic_json(root / "monitor_run_report.json", {"generated_at": now(), "reports": reports})
    if reports:
        record_reports(root, reports, started, config_kinds(config))
    return reports
