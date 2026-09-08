#!/usr/bin/env python3
"""Run the migrated price monitors against a self-hosted Firecrawl API.

The deployed Firecrawl instance exposes scrape/crawl endpoints but not the
cloud Monitor API. This runner keeps an immutable-ish per-monitor snapshot,
detects price-bearing changes, and only sends the configured webhook when
notifications are explicitly enabled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "firecrawl_monitor_migration.json"
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


def compare_facts(previous: Iterable[str], current: Iterable[str]) -> dict[str, list[str]]:
    old = set(previous)
    new = set(current)
    return {"added": sorted(new - old), "removed": sorted(old - new)}


def response_markdown(payload: dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    body = data if isinstance(data, dict) else payload
    return str(body.get("markdown") or "") if isinstance(body, dict) else ""


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "monitor"


class SelfHostedMonitor:
    def __init__(self, api_url: str, api_key: str, timeout: float) -> None:
        self.api_url = api_url.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout

    def scrape(self, url: str) -> str:
        response = requests.post(
            f"{self.api_url}/v1/scrape",
            headers=self.headers,
            json={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is False:
            raise RuntimeError(str(payload.get("error") or "firecrawl_scrape_failed"))
        markdown = response_markdown(payload)
        if not markdown.strip():
            raise RuntimeError("firecrawl_empty_markdown")
        return markdown


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
        for attempt in range(2):
            try:
                facts = extract_price_facts(monitor.scrape(url))
                status = "new" if not previous else ("changed" if tuple(previous.get("facts", [])) != facts else "same")
                return PageResult(url=url, status=status, facts=facts)
            except Exception as exc:  # one bad page must not stop the batch
                last_error = str(exc)[:500]
                if attempt == 0:
                    time.sleep(1)
        return PageResult(url=url, status="error", facts=tuple(), error=last_error)

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
) -> dict[str, Any]:
    definition = config["monitors"][name]
    urls = [url for target in definition.get("targets", []) for url in target.get("urls", [])]
    urls = urls[:limit] if limit else urls
    state_path = state_dir / f"{safe_name(name)}.json"
    old_state = load_state(state_path)
    old_pages = (old_state or {}).get("pages", {})
    pages = build_page_results(SelfHostedMonitor(api_url, api_key, timeout), urls, old_pages, workers=workers)
    baseline = old_state is None or bool(old_state.get("baseline", False) and not old_state.get("baseline_complete", False))
    snapshot = build_snapshot(
        name,
        pages,
        baseline=baseline,
        baseline_complete=len(pages) == len(urls) and all(page.status != "error" for page in pages),
        old_pages=old_pages,
    )
    events = build_events(name, pages, old_pages, baseline=baseline)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    webhook = ((definition.get("webhook") or {}).get("url") or "").strip()
    notified = False
    if send_notifications and webhook and events:
        post_webhook(webhook, {"source": "costfinder_self_hosted_monitor", "events": events}, timeout)
        notified = True
    return {
        "monitor_name": name,
        "baseline": baseline,
        "total_pages": len(pages),
        "same": sum(page.status == "same" for page in pages),
        "new": sum(page.status == "new" for page in pages),
        "changed": sum(page.status == "changed" for page in pages),
        "error": sum(page.status == "error" for page in pages),
        "meaningful_events": len(events),
        "webhook_notified": notified,
        "page_errors": [{"url": page.url, "error": page.error} for page in pages if page.status == "error"],
        "state_path": str(state_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--monitor", action="append", dest="monitors")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=float(os.getenv("FIRECRAWL_HTTP_TIMEOUT_SECS", "120")))
    parser.add_argument("--workers", type=int, default=int(os.getenv("FIRECRAWL_MONITOR_WORKERS", "5")))
    parser.add_argument("--api-url", default=os.getenv("FIRECRAWL_SELF_HOSTED_API_URL") or os.getenv("FIRECRAWL_API_URL") or "http://127.0.0.1:3002")
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
    reports = [run_monitor(config, name, api_url=args.api_url, api_key=args.api_key, state_dir=args.state_dir, timeout=args.timeout, limit=args.limit, workers=args.workers, send_notifications=args.send_notifications) for name in names]
    print(json.dumps({"generated_at": utc_now(), "reports": reports}, ensure_ascii=False, indent=2))
    return 0 if all(report["error"] == 0 for report in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
