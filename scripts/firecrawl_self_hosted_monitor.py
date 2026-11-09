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
from utils.monitor_price_store import atomic_json, digest, run_lock
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
                "formats": ["markdown", "rawHtml"] if crawler_first_enabled() else ["markdown"],
                "onlyMainContent": True,
                "excludeTags": ["nav", "footer", "script", "style", "noscript"],
                "fastMode": True,
            },
            timeout=self.timeout,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"http_status": response.status_code, "body": response.text[:2000]}
        data = payload.get("data") if response.ok and isinstance(payload.get("data"), dict) else None
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


def crawler_first_enabled() -> bool:
    """Opt-in: reuse an earlier real render when crawler content is unchanged and cleanly visible."""
    return os.getenv("MONITOR_RENDER_MODE", "always").strip().lower() == "crawler_first"


def reusable_render(url: str, page: Any, previous: dict[str, Any], state_dir: Path, now_ts: str) -> dict[str, Any] | None:
    """Return the previous rendered evidence when this capture needs no new browser render.

    Reuse requires: crawler content identical to the last rendered capture, the filtered crawler
    HTML assessed as cleanly visible (``assess_page`` finds no reason to need a browser), and the
    last real render younger than ``MONITOR_RENDER_MAX_AGE_DAYS`` (default 14). Anything else
    (new/changed page, empty or suspicious content, old render) is rendered in the browser.
    """
    from utils.crawler_visibility import assess_page

    payload = page.response or {}
    raw_html = ((payload.get("data") or {}).get("rawHtml") if isinstance(payload, dict) else None) or ""
    if page.status == "error" or not previous.get("artifact_id") or not previous.get("rendered_at"):
        return None
    if previous.get("content_digest") != digest(page.markdown):
        return None
    try:
        age_days = (datetime.fromisoformat(now_ts) - datetime.fromisoformat(previous["rendered_at"])).days
    except (TypeError, ValueError):
        return None
    if age_days > int(os.getenv("MONITOR_RENDER_MAX_AGE_DAYS", "14")):
        return None
    assessment = assess_page(page.markdown, raw_html, facts=page.facts)
    if assessment.needs_browser:
        return None
    evidence_path = state_dir / "evidence" / (str(previous["artifact_id"]) + ".json")
    try:
        rendered = json.loads(evidence_path.read_text(encoding="utf-8")).get("rendered") or {}
    except (OSError, ValueError):
        return None
    browser = rendered.get("browser") or {}
    if not browser.get("accessible") or not browser.get("visual_digest"):
        return None
    return {**rendered, "reused_from_artifact": previous["artifact_id"], "reused_at": now_ts,
            "visibility_source": "crawler_html_filtered", "crawler_visible_digest": assessment.visible_digest}


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
                                  failed_attempts=tuple(failed_attempts))
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
    urls = [url for target in definition.get("targets", []) for url in target.get("urls", [])]
    if urls_filter is not None:
        urls = [url for url in urls if url in urls_filter]
    urls = urls[:limit] if limit else urls
    state_path = state_dir / f"{safe_name(name)}.json"
    old_state = load_state(state_path)
    old_pages = (old_state or {}).get("pages", {})
    browser_results = {}
    monitor = SelfHostedMonitor(api_url, api_key, timeout)
    if scan_images:
        from utils.monitor_rendered_browser import render_page
        from utils.monitor_rendered_checks import confirmed_not_found
        cache = browser_cache if browser_cache is not None else {}
        def capture_one(url):
            page=build_page_results(monitor,[url],old_pages,workers=1)[0]
            reuse = reusable_render(url, page, old_pages.get(url) or {}, state_dir, utc_now()) if crawler_first_enabled() else None
            if crawler_first_enabled() and isinstance(page.response, dict) and isinstance(page.response.get("data"), dict):
                page.response["data"].pop("rawHtml", None)  # assessed above; keep evidence files small
            if reuse is not None:
                result = reuse
                cache[url] = result
            elif url in cache:
                result=cache[url]
            else:
                directory=state_dir / "rendered" / uuid4().hex
                try:
                    browser=render_page(url,directory)
                    result={"browser":browser,"browser_path":str(directory / "browser.json")}
                except Exception as exc:
                    result={"error":type(exc).__name__,"browser_path":str(directory / "browser.json")}
                    atomic_json(directory / "capture_failure.json",result)
                cache[url]=result
            browser=result.get("browser",{})
            if page.status=="error" and (browser.get("accessible") or confirmed_not_found(browser)):
                text=browser.get("text", "")
                page=replace(page,status="new",error=None,markdown=text,facts=extract_price_facts(text))
            return page,result
        with ThreadPoolExecutor(max_workers=2) as executor:
            captured=list(executor.map(capture_one,urls))
        pages=[page for page,_ in captured]
        browser_results={page.url:result for page,result in captured}
    else:
        pages=build_page_results(monitor,urls,old_pages,workers=workers)
    # Each successfully captured page establishes its own baseline. A failing
    # sibling must not suppress future changes on pages already captured.
    baseline = old_state is None
    snapshot = build_snapshot(
        name,
        pages,
        baseline=baseline,
        baseline_complete=len(pages) == len(urls) and all(page.status != "error" for page in pages),
        old_pages=old_pages,
    )
    events = build_events(name, pages, old_pages, baseline=baseline and not catchup)
    state_dir.mkdir(parents=True, exist_ok=True)
    event_by_url = {event["url"]: event for event in events}
    for page in pages:
        if page.failed_attempts:
            captured_at = utc_now()
            atomic_json(state_dir / "failed_evidence" / (uuid4().hex+".json"),
                        {"url":page.url,"captured_at":captured_at,"error":page.error,
                         "failed_attempts":list(page.failed_attempts)})
        if page.status == "error":
            continue
        rendered = browser_results.get(page.url,{})
        browser = rendered.get("browser",{})
        previous = old_pages.get(page.url) or {}
        # Full markdown also detects changed dates, qualification, and removal
        # of image links, which price-line comparison alone cannot establish.
        content_digest = digest(page.markdown)
        # Keep each raw response even when rendered Markdown is unchanged.
        artifact_id = uuid4().hex
        artifact_path = state_dir / "evidence" / (artifact_id + ".json")
        atomic_json(artifact_path, {"url": page.url, "captured_at": utc_now(),
                    "markdown": page.markdown, "raw_response": page.response,
                    "failed_attempts":list(page.failed_attempts),"facts": list(page.facts),
                    "rendered":rendered,"browser_fallback":bool(page.failed_attempts and browser.get("accessible"))})
        snapshot["pages"][page.url].update(content_digest=content_digest, artifact_id=artifact_id)
        if browser.get("accessible"):
            snapshot["pages"][page.url].update(visual_digest=browser.get("visual_digest"),context_digest=browser.get("context_digest"))
            if rendered.get("reused_from_artifact"):
                snapshot["pages"][page.url]["rendered_at"] = previous.get("rendered_at")
            else:
                snapshot["pages"][page.url]["rendered_at"] = utc_now()
        if previous.get("revision"):
            snapshot["pages"][page.url]["revision"] = previous["revision"]
        event = event_by_url.get(page.url)
        if catchup and not previous.get("sync_enqueued"):
            event = {"monitor_name": name, "url": page.url, "status": "catchup",
                     "price_diff": compare_facts(previous.get("facts", []), page.facts)}
        elif previous.get("content_digest") and previous["content_digest"] != content_digest:
            event = event or {"monitor_name": name, "url": page.url, "status": "content_changed",
                             "price_diff": compare_facts(previous.get("facts", []), page.facts)}
        if (scan_images and browser.get("accessible") and
            (browser.get("visual_digest") != previous.get("visual_digest") or
             browser.get("context_digest") != previous.get("context_digest"))):
            event = event or {"monitor_name":name,"url":page.url,"status":"visible_content_changed",
                             "price_diff":compare_facts(previous.get("facts", []),page.facts)}
        if scan_images and not browser.get("accessible") and not event:
            # Text events remain independent; missing visual proof is retried per page.
            event={"monitor_name":name,"url":page.url,"status":"browser_capture_required","price_diff":{}}
        if event:
            event_id = digest([name, page.url, previous.get("revision", previous.get("artifact_id")), artifact_id])
            event_path = state_dir / "events" / (event_id + ".json")
            if not event_path.exists():
                atomic_json(event_path, {**event, "event_id": event_id, "status": "pending",
                            "change_type": event["status"], "created_at": utc_now(),
                            "artifact_id": artifact_id, "artifact_path": str(artifact_path)})
            snapshot["pages"][page.url]["sync_enqueued"] = True
            snapshot["pages"][page.url]["revision"] = event_id
        elif previous.get("sync_enqueued"):
            snapshot["pages"][page.url]["sync_enqueued"] = True
    # Durable evidence + pending event must precede advancing the page snapshot.
    atomic_json(state_path, snapshot)
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
        "successful_urls": [page.url for page in pages if page.status != "error"],
        "changed_urls": [page.url for page in pages if page.status == "changed"],
        "browser_coverage": [{"url":url,"accessible":row.get("browser",{}).get("accessible",False),
            "complete":row.get("browser",{}).get("complete",False),"http_status":row.get("browser",{}).get("http_status"),
            "image_review_required":row.get("browser",{}).get("image_review_required"),"browser_path":row["browser_path"],
            "error":row.get("error")} for url,row in browser_results.items()],
        "state_path": str(state_path),
    }


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
