#!/usr/bin/env python3
"""Turn one monitor run's state directory into ``crawl_scheduler.py record`` results.

For every business in ``due.json``: ``ok`` is true when at least one of its URLs was captured since
``--since`` and ``changed`` when any captured URL reports ``changed`` or has a content-change event.
A business is reported ``ok=false`` only when one of its URLs was attempted and failed; a business
none of whose URLs were attempted (for example a ``--limit`` run) produces no result and stays due.
Read-only on the monitor state; writes only ``--out``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.firecrawl_scrape_raw_db import canonical_scrape_url  # noqa: E402

CHANGE_EVENT_STATUSES = {"content_changed", "visible_content_changed"}


def _canon(urls: Iterable[str]) -> set[str]:
    return {canonical_scrape_url(url) for url in urls if canonical_scrape_url(url)}


def results_from_monitor(
    due_businesses: Iterable[Mapping[str, Any]],
    *,
    captured_pages: Mapping[str, Mapping[str, Any]],
    event_urls: set[str],
    failed_urls: set[str] = frozenset(),  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    """``captured_pages`` maps canonical URL to its monitor page entry (only URLs captured this run)."""
    results = []
    for business in due_businesses:
        urls = _canon(business.get("urls", ()))
        captured = [url for url in urls if url in captured_pages]
        if not captured and not (urls & failed_urls):
            continue  # never attempted this run: leave the business due, do not count a failure
        changed = any(
            captured_pages[url].get("last_status") == "changed" or url in event_urls for url in captured
        )
        results.append({"business_id": int(business["business_id"]), "ok": bool(captured), "changed": changed})
    return results


def _since_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_monitor_run(
    state_dir: Path, monitor_names: Iterable[str], since: datetime
) -> tuple[dict, set[str], set[str]]:
    """Pages from snapshots updated since ``since``, URLs with change events and URLs that failed."""
    pages: dict[str, dict[str, Any]] = {}
    for name in monitor_names:
        path = state_dir / f"{name}.json"
        if not path.exists():
            continue
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        updated = snapshot.get("updated_at")
        if not updated or _since_ts(updated) < since:
            continue
        for url, entry in (snapshot.get("pages") or {}).items():
            pages[canonical_scrape_url(url)] = entry
    event_urls: set[str] = set()
    events_dir = state_dir / "events"
    if events_dir.is_dir():
        for path in events_dir.glob("*.json"):
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < since:
                continue
            event = json.loads(path.read_text(encoding="utf-8"))
            if event.get("status") in CHANGE_EVENT_STATUSES and event.get("url"):
                event_urls.add(canonical_scrape_url(event["url"]))
    failed_urls: set[str] = set()
    failed_dir = state_dir / "failed_evidence"
    if failed_dir.is_dir():
        for path in failed_dir.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            captured_at = record.get("captured_at")
            if record.get("url") and captured_at and _since_ts(captured_at) >= since:
                failed_urls.add(canonical_scrape_url(record["url"]))
    return pages, event_urls, failed_urls


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True, help="monitor --state-dir of the run")
    parser.add_argument("--due", type=Path, required=True, help="due.json from crawl_scheduler.py plan --apply")
    parser.add_argument("--monitors", required=True, help="comma-separated monitor names that were run")
    parser.add_argument("--since", required=True, help="ISO timestamp of the run start")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    due = json.loads(args.due.read_text(encoding="utf-8"))
    pages, event_urls, failed_urls = load_monitor_run(args.state_dir, args.monitors.split(","), _since_ts(args.since))
    results = results_from_monitor(
        due.get("businesses", []), captured_pages=pages, event_urls=event_urls, failed_urls=failed_urls
    )
    args.out.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    ok = sum(1 for r in results if r["ok"])
    skipped = len(due.get("businesses", [])) - len(results)
    print(f"businesses={len(results)} ok={ok} failed={len(results) - ok} "
          f"changed={sum(1 for r in results if r['changed'])} not_attempted={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
