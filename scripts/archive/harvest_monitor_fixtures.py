#!/usr/bin/env python3
"""
Harvest Firecrawl monitor check pages with meaningful changes as test fixtures.

Exports page dicts (url, status, diff, judgment) compatible with
utils.change_driven_extractor.extract_diff_payload.

Usage:
    python scripts/harvest_monitor_fixtures.py --dry-run --limit-monitors 5
    python scripts/harvest_monitor_fixtures.py --max-fixtures 10
    python scripts/harvest_monitor_fixtures.py --monitor-id <id> --limit-checks-per-monitor 3

Environment:
    FIRECRAWL_API_KEY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.promo_site_crawler import normalize_domain
from scripts.firecrawl_monitor_poll import (
    _obj_to_dict,
    check_has_changes,
    fetch_meaningful_pages,
    list_all_monitors,
    list_monitor_checks,
    page_is_meaningful,
    sort_checks_newest_first,
)
from utils.firecrawl_client import get_firecrawl_client

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "tests" / "fixtures" / "monitor_cases"
_CONF_RANK = {"low": 1, "medium": 2, "high": 3}


def load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def _confidence_of(page: Dict[str, Any]) -> str:
    judgment = page.get("judgment") or {}
    if not isinstance(judgment, dict):
        judgment = _obj_to_dict(judgment)
    return str(judgment.get("confidence") or "low").strip().lower()


def _page_meets_confidence(page: Dict[str, Any], min_confidence: str) -> bool:
    return _CONF_RANK.get(_confidence_of(page), 1) >= _CONF_RANK.get(min_confidence, 1)


def _domain_slug(page: Dict[str, Any]) -> str:
    domain = normalize_domain(page.get("url") or "")
    if not domain:
        return "unknown"
    return re.sub(r"[^a-z0-9.-]+", "_", domain.lower())


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]


def _fixture_filename(page: Dict[str, Any], check_id: str) -> str:
    check_short = (check_id or "check")[:8]
    url = page.get("url") or "page"
    return f"{_domain_slug(page)}__{check_short}__{_url_hash(url)}.json"


def _page_to_fixture(page: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only fields needed by extract_diff_payload."""
    page_dict = _obj_to_dict(page)
    judgment = page_dict.get("judgment") or {}
    if not isinstance(judgment, dict):
        judgment = _obj_to_dict(judgment)

    diff = page_dict.get("diff") or {}
    if not isinstance(diff, dict):
        diff = _obj_to_dict(diff)

    fixture: Dict[str, Any] = {
        "url": page_dict.get("url") or "",
        "status": page_dict.get("status") or "",
        "diff": {
            "text": diff.get("text") or "",
            "json": diff.get("json") or {},
        },
        "judgment": {
            "meaningful": judgment.get("meaningful"),
            "confidence": judgment.get("confidence"),
            "reason": judgment.get("reason") or "",
            "meaningfulChanges": (
                judgment.get("meaningfulChanges")
                or judgment.get("meaningful_changes")
                or []
            ),
        },
    }
    return fixture


def _has_json_diff(page: Dict[str, Any]) -> bool:
    diff = page.get("diff") or {}
    if not isinstance(diff, dict):
        diff = _obj_to_dict(diff)
    json_diff = diff.get("json")
    return bool(json_diff)


def harvest_fixtures(
    fc,
    *,
    monitors: List[Dict[str, Any]],
    limit_checks_per_monitor: int,
    max_fixtures: int,
    min_confidence: str,
    delay_secs: float,
    dry_run: bool,
    output_dir: Path,
) -> Dict[str, Any]:
    harvested_at = datetime.now(timezone.utc).isoformat()
    manifest_entries: List[Dict[str, Any]] = []
    written = 0
    api_calls = 0

    for monitor in monitors:
        if written >= max_fixtures:
            break

        monitor_id = monitor.get("id") or monitor.get("monitorId") or ""
        monitor_name = monitor.get("name") or monitor_id
        if not monitor_id:
            continue

        checks = sort_checks_newest_first(list_monitor_checks(fc, monitor_id))
        api_calls += 1
        changed_checks = [check for check in checks if check_has_changes(check)][
            : max(1, limit_checks_per_monitor)
        ]

        for check in changed_checks:
            if written >= max_fixtures:
                break

            check_id = check.get("id") or ""
            if not check_id:
                continue

            if delay_secs > 0 and api_calls > 1:
                time.sleep(delay_secs)

            pages, _ = fetch_meaningful_pages(fc, monitor_id, check_id)
            api_calls += 2  # changed + new status fetches (approx)

            for page in pages:
                if written >= max_fixtures:
                    break
                if not page_is_meaningful(page):
                    continue
                if not _page_meets_confidence(page, min_confidence):
                    continue

                fixture = _page_to_fixture(page)
                filename = _fixture_filename(page, check_id)
                entry = {
                    "filename": filename,
                    "monitor_id": monitor_id,
                    "monitor_name": monitor_name,
                    "check_id": check_id,
                    "url": fixture.get("url"),
                    "confidence": _confidence_of(page),
                    "has_json_diff": _has_json_diff(page),
                    "harvested_at": harvested_at,
                }
                manifest_entries.append(entry)

                if dry_run:
                    written += 1
                    continue

                output_dir.mkdir(parents=True, exist_ok=True)
                out_path = output_dir / filename
                out_path.write_text(
                    json.dumps(fixture, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                written += 1

    manifest = {
        "harvested_at": harvested_at,
        "dry_run": dry_run,
        "count": len(manifest_entries),
        "min_confidence": min_confidence,
        "entries": manifest_entries,
    }

    if not dry_run and manifest_entries:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    elif dry_run:
        manifest_path = output_dir / "manifest.dry_run.json"
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest["manifest_path"] = str(manifest_path)

    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harvest meaningful Firecrawl monitor pages as test fixtures"
    )
    parser.add_argument("--limit-monitors", type=int, default=None)
    parser.add_argument("--limit-checks-per-monitor", type=int, default=5)
    parser.add_argument("--max-fixtures", type=int, default=30)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Fixture output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--dry-run", action="store_true", help="Write manifest only, no fixture files")
    parser.add_argument("--monitor-id", default=None, help="Harvest from a single monitor")
    parser.add_argument(
        "--min-confidence",
        default="low",
        choices=["low", "medium", "high"],
    )
    parser.add_argument(
        "--delay-secs",
        type=float,
        default=21.0,
        help="Delay between monitor/check API calls (Firecrawl rate limit)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env()
    fc = get_firecrawl_client()

    monitors = list_all_monitors(fc)
    if args.monitor_id:
        monitors = [
            monitor
            for monitor in monitors
            if (monitor.get("id") or monitor.get("monitorId")) == args.monitor_id
        ]
    if args.limit_monitors is not None:
        monitors = monitors[: args.limit_monitors]

    if not monitors:
        print("No monitors matched the selection.")
        return

    manifest = harvest_fixtures(
        fc,
        monitors=monitors,
        limit_checks_per_monitor=max(1, args.limit_checks_per_monitor),
        max_fixtures=max(1, args.max_fixtures),
        min_confidence=args.min_confidence,
        delay_secs=max(0.0, args.delay_secs),
        dry_run=bool(args.dry_run),
        output_dir=args.output_dir,
    )

    summary = {
        "monitors_seen": len(monitors),
        "fixtures_found": manifest["count"],
        "dry_run": manifest["dry_run"],
        "output_dir": str(args.output_dir),
    }
    if manifest.get("manifest_path"):
        summary["manifest_path"] = manifest["manifest_path"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
