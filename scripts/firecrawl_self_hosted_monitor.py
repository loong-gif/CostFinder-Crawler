#!/usr/bin/env python3
"""Run the migrated price monitors against a self-hosted Firecrawl API.

The deployed Firecrawl instance exposes scrape/crawl endpoints but not the
cloud Monitor API. This runner keeps an immutable-ish per-monitor snapshot,
detects price-bearing changes, and only sends the configured webhook when
notifications are explicitly enabled.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from utils.ego_browser import BrowserScrapeFailed, ego_scrape
from utils.firecrawl_client import fetched_content, page_gone
from utils.service_endpoints import FIRECRAWL_URL
from utils.monitor_capture_record import compare_facts, monitor_report, record_page
from utils.monitor_page_visibility import page_visibility
from utils.monitor_price_store import atomic_json, run_lock
from utils.monitor_rendered_checks import confirmed_not_found
DEFAULT_STATE_DIR = PROJECT_ROOT / "output" / "self_hosted_monitor_state"

PRICE_LINE_RE = re.compile(
    r"(?:\$\s*\d[\d,]*(?:\.\d{1,2})?|\b\d[\d,]*(?:\.\d{1,2})?\s*%|"
    r"\b\d[\d,]*(?:\.\d{1,2})?\s*(?:usd|per\s+unit|/\s*unit|units?|syringes?|areas?|sessions?))",
    re.IGNORECASE,
)
PRICE_CONTEXT_RE = re.compile(
    r"\b(?:price|pricing|special|promotion|offer|discount|sale|regular|original|"
    r"membership|package|unit|syringe|area|session|save|free\s+units?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PageResult:
    url: str
    status: str
    facts: tuple[str, ...]
    error: str | None = None
    markdown: str = ""
    response: dict[str, Any] | None = None
    failed_attempts: tuple[dict[str, Any], ...] = ()
    captured_at: str = ""


class ScrapeFailure(RuntimeError):
    def __init__(self, reason: str, raw_response: dict[str, Any]):
        super().__init__(reason)
        self.raw_response = raw_response


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def extract_price_facts(markdown: str) -> tuple[str, ...]:
    """Return stable, price-bearing lines; ignore non-price copy changes."""
    facts: set[str] = set()
    for raw_line in str(markdown or "").splitlines():
        line = normalize_line(raw_line)
        if not line or not PRICE_LINE_RE.search(line):
            continue
        if PRICE_CONTEXT_RE.search(line):
            facts.add(line)
    return tuple(sorted(facts))


def response_markdown(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    body = data if isinstance(data, dict) else payload
    return str(body.get("markdown") or "") if isinstance(body, dict) else ""


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "monitor"


def fast_mode_result(response: requests.Response) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """fastMode 的响应体与其中的 data；响应不是 JSON 时保存状态码和正文开头。"""
    try:
        payload = response.json()
    except ValueError:
        payload = {"http_status": response.status_code, "body": response.text[:2000]}
    return payload, payload.get("data") if response.ok and isinstance(payload.get("data"), dict) else None


class SelfHostedMonitor:
    def __init__(self, api_url: str, api_key: str, timeout: float) -> None:
        self.api_url = api_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout

    def scrape_document(self, url: str) -> dict[str, Any]:
        """先 Firecrawl fastMode；拿不到正文（请求失败、拦截页、空正文）时由 ego-lite 渲染。

        Firecrawl 的浏览器引擎（容器内 Chromium）在本主机的 WSL1 上会卡住并把其他页面的内容返回给当前请求，
        不再使用。404/410 是页面下架的证据，照常返回 Firecrawl 的结果。
        """
        response = requests.post(
            f"{self.api_url}/v1/scrape",
            headers=self.headers,
            json={
                "url": url,
                "formats": ["markdown", "rawHtml"],
                "onlyMainContent": True,
                "excludeTags": ["nav", "footer", "script", "style", "noscript"],
                "fastMode": True,
            },
            timeout=self.timeout,
        )
        payload, data = fast_mode_result(response)
        if data is not None and page_gone(data):
            if not response_markdown(payload).strip():
                raise ScrapeFailure("firecrawl_empty_markdown", payload)
            return payload
        if data is not None and fetched_content(data):
            return payload
        try:
            rendered = ego_scrape(url)
        except BrowserScrapeFailed as exc:
            raise ScrapeFailure("ego_scrape_failed", {"fast_mode": {"http_status": response.status_code, "body": payload},
                                                      "ego_error": str(exc)}) from None
        return {"success": True, "data": rendered}

    def scrape(self, url: str) -> str:
        return response_markdown(self.scrape_document(url))


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported monitor config schema_version")
    if not isinstance(payload.get("monitors"), dict) or not payload["monitors"]:
        raise ValueError("monitor config contains no monitors")
    return payload


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_page_results(
    monitor: SelfHostedMonitor,
    urls: list[str],
    old_pages: dict[str, Any],
    workers: int = 5,
) -> tuple[PageResult, ...]:
    def scrape_one(url: str) -> PageResult:
        previous = old_pages.get(url) or {}
        last_error = "firecrawl_scrape_failed"
        failed_attempts = []
        for attempt in range(2):
            try:
                if hasattr(monitor, "scrape_document"):
                    payload = monitor.scrape_document(url)
                    markdown = response_markdown(payload)
                else:
                    markdown, payload = monitor.scrape(url), None
                facts = extract_price_facts(markdown)
                status = "new" if not previous else ("changed" if tuple(previous.get("facts", [])) != facts else "same")
                return PageResult(url=url, status=status, facts=facts, markdown=markdown, response=payload,
                                  failed_attempts=tuple(failed_attempts), captured_at=utc_now())
            except Exception as exc:  # one bad page must not stop the batch
                last_error = str(exc)[:500]
                if isinstance(exc, requests.RequestException):
                    last_error = type(exc).__name__
                failed_attempts.append({"error":last_error,"raw_response":getattr(exc,"raw_response",None)})
                if attempt == 0:
                    time.sleep(1)
        return PageResult(url=url, status="error", facts=tuple(), error=last_error,
                          failed_attempts=tuple(failed_attempts))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        return tuple(executor.map(scrape_one, urls))


def build_snapshot(
    name: str,
    pages: Iterable[PageResult],
    *,
    baseline: bool,
    baseline_complete: bool,
    old_pages: dict[str, Any] | None = None,
) -> dict[str, Any]:
    page_map = {url: dict(value) for url, value in (old_pages or {}).items()}
    page_map.update({
        page.url: {
            "facts": list(page.facts),
            "last_status": page.status,
            "last_error": page.error,
        }
        for page in pages
        if page.status != "error"
    })
    return {
        "schema_version": 1,
        "monitor_name": name,
        "updated_at": utc_now(),
        "baseline": baseline,
        "baseline_complete": baseline_complete,
        "pages": page_map,
    }


def build_events(name: str, pages: Iterable[PageResult], old_pages: dict[str, Any], *, baseline: bool) -> list[dict[str, Any]]:
    if baseline:
        return []
    events: list[dict[str, Any]] = []
    for page in pages:
        if page.status not in {"new", "changed"}:
            continue
        diff = compare_facts((old_pages.get(page.url) or {}).get("facts", []), page.facts)
        if not diff["added"] and not diff["removed"]:
            continue
        events.append({"monitor_name": name, "url": page.url, "status": page.status, "price_diff": diff})
    return events


def post_webhook(url: str, payload: dict[str, Any], timeout: float) -> None:
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()


def run_monitor(
    config: dict[str, Any],
    name: str,
    *,
    api_url: str,
    api_key: str,
    state_dir: Path,
    timeout: float,
    limit: int | None,
    workers: int,
    send_notifications: bool,
    catchup: bool = False,
    urls_filter: set[str] | None = None,
    scan_images: bool = False,
    browser_cache: dict | None = None,
) -> dict[str, Any]:
    definition = config["monitors"][name]
    urls = monitor_urls(definition, urls_filter, limit)
    state_path = state_dir / f"{safe_name(name)}.json"
    old_state = load_state(state_path)
    old_pages = (old_state or {}).get("pages", {})
    monitor = SelfHostedMonitor(api_url, api_key, timeout)
    # 不核验可见性的监控组（会员页）只抓取，browser_results 为 None
    pages, browser_results = (capture_visible_pages(monitor, urls, old_pages, state_dir, browser_cache) if scan_images
                              else (build_page_results(monitor, urls, old_pages, workers=workers), None))
    # Each successfully captured page establishes its own baseline. A failing
    # sibling must not suppress future changes on pages already captured.
    baseline = old_state is None
    snapshot = build_snapshot(
        name,
        pages,
        baseline=baseline,
        baseline_complete=len(pages) == len(urls) and "error" not in {page.status for page in pages},
        old_pages=old_pages,
    )
    events = build_events(name, pages, old_pages, baseline=baseline and not catchup)
    state_dir.mkdir(parents=True, exist_ok=True)
    record_pages(name, pages, browser_results, old_pages, snapshot, events, state_dir, catchup=catchup)
    # Durable evidence + pending event must precede advancing the page snapshot.
    atomic_json(state_path, snapshot)
    notified = notify(definition, events, send_notifications, timeout)
    return monitor_report(name, baseline, pages, events, notified, browser_results or {}, state_path)


def monitor_urls(definition: dict[str, Any], urls_filter: set[str] | None, limit: int | None) -> list[str]:
    urls = [url for target in definition.get("targets", []) for url in target.get("urls", [])]
    if urls_filter is not None:
        urls = [url for url in urls if url in urls_filter]
    return urls[:limit] if limit else urls


def record_pages(name: str, pages: list[PageResult], browser_results: dict[str, dict] | None,
                 old_pages: dict[str, Any], snapshot: dict[str, Any], events: list[dict[str, Any]], state_dir: Path,
                 *, catchup: bool) -> None:
    event_by_url = {event["url"]: event for event in events}
    for page in pages:
        record_failed_attempts(page, state_dir)
        if page.status != "error":
            rendered = None if browser_results is None else browser_results[page.url]
            record_page(name, page, rendered, old_pages.get(page.url) or {}, snapshot["pages"][page.url],
                        state_dir, event_by_url.get(page.url), catchup=catchup)


def record_failed_attempts(page: PageResult, state_dir: Path) -> None:
    if page.failed_attempts:
        atomic_json(state_dir / "failed_evidence" / (uuid4().hex+".json"),
                    {"url":page.url,"captured_at":utc_now(),"error":page.error,
                     "failed_attempts":list(page.failed_attempts)})


def notify(definition: dict[str, Any], events: list[dict[str, Any]], send_notifications: bool, timeout: float) -> bool:
    webhook = ((definition.get("webhook") or {}).get("url") or "").strip()
    notified = bool(send_notifications and webhook and events)
    if notified:
        post_webhook(webhook, {"source": "costfinder_self_hosted_monitor", "events": events}, timeout)
    return notified


def capture_visible_pages(monitor: SelfHostedMonitor, urls: list[str], old_pages: dict[str, Any], state_dir: Path,
                          cache: dict | None) -> tuple[list[PageResult], dict[str, dict]]:
    """逐页抓取并取得可见性证据；抓取失败而渲染可访问（或确认 404）的页面改用渲染出的文字。

    cache 由同一轮运行的各监控组共用，同一地址只判定一次。
    """
    cache = {} if cache is None else cache
    def capture_one(url):
        page = build_page_results(monitor, [url], old_pages, workers=1)[0]
        result = page_visibility(url, page, state_dir, cache)
        browser = result.get("browser", {})
        if page.status == "error" and (browser.get("accessible") or confirmed_not_found(browser)):
            text = browser.get("text", "")
            page = replace(page, status="new", error=None, markdown=text, facts=extract_price_facts(text))
        return page, result
    with ThreadPoolExecutor(max_workers=2) as executor:
        captured = list(executor.map(capture_one, urls))
    return [page for page, _ in captured], {page.url: result for page, result in captured}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--monitor", action="append", dest="monitors")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=float(os.getenv("FIRECRAWL_HTTP_TIMEOUT_SECS", "120")))
    parser.add_argument("--workers", type=int, default=int(os.getenv("FIRECRAWL_MONITOR_WORKERS", "5")))
    parser.add_argument("--api-url", default=FIRECRAWL_URL)
    parser.add_argument("--api-key", default=os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY") or os.getenv("FIRECRAWL_API_KEY") or "")
    parser.add_argument("--send-notifications", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    names = args.monitors or list(config["monitors"])
    unknown = sorted(set(names) - set(config["monitors"]))
    if unknown:
        raise SystemExit(f"unknown monitor(s): {', '.join(unknown)}")
    with run_lock(args.state_dir):
        reports = [run_monitor(config, name, api_url=args.api_url, api_key=args.api_key, state_dir=args.state_dir, timeout=args.timeout, limit=args.limit, workers=args.workers, send_notifications=args.send_notifications) for name in names]
    print(json.dumps({"generated_at": utc_now(), "reports": reports}, ensure_ascii=False, indent=2))
    return 0 if all(report["error"] == 0 for report in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
