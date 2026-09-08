#!/usr/bin/env python3
"""Backfill missing Firecrawl source links for promotions and memberships."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.rescrape_firecrawl_raw_only_main import request_v1_scrape
from utils.direct_public_scrape import scrape_public_page_text
from utils.firecrawl_scrape_raw_db import (
    canonical_scrape_url,
    save_scrape_response,
    scrape_request_fingerprint,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key

TARGET_TABLES = ("clinic_promotions", "clinic_memberships")
TARGET_SELECT = {
    "clinic_promotions": "promotion_id,business_id,source_url,scrape_id",
    "clinic_memberships": "plan_id,business_id,source_url,scrape_id",
}
RAW_SELECT = (
    "id,source_url,success,updated_at"
)
SCRAPE_FORMATS = ("markdown", "links")
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
_PROXY_LOCK = Lock()


@contextmanager
def without_http_proxy():
    """Self-hosted Firecrawl should bypass project-level proxy settings."""
    with _PROXY_LOCK:
        saved = {key: os.environ.pop(key, None) for key in _PROXY_KEYS}
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_id(table_name: str, row: dict[str, Any]) -> int:
    key = "promotion_id" if table_name == "clinic_promotions" else "plan_id"
    return int(row[key])


def _response_data(raw: dict[str, Any]) -> dict[str, Any]:
    value = raw.get("response_json")
    if not isinstance(value, dict):
        return {}
    data = value.get("data")
    return data if isinstance(data, dict) else value


def is_usable_raw(raw: dict[str, Any]) -> bool:
    """Return whether a raw row contains successful Firecrawl source evidence."""
    if raw.get("success") is not True:
        return False
    if raw.get("source_evidence_present") is True:
        return True
    data = _response_data(raw)
    return any(
        str(raw.get(field) or data.get(field) or "").strip()
        for field in ("markdown", "html", "raw_html", "screenshot")
    ) or bool(raw.get("response_json"))


def choose_latest_usable_raw(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in rows if is_usable_raw(row)]
    if not usable:
        raise ValueError("no usable raw rows")
    return max(usable, key=lambda row: (str(row.get("updated_at") or ""), int(row["id"])))


def group_targets_by_url(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        url = canonical_scrape_url(str(row.get("source_url") or ""))
        if not url:
            continue
        grouped.setdefault(url, []).append(row)
    return grouped


def fetch_targets(db: Any) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for table_name in TARGET_TABLES:
        rows = db.fetch_rows(
            table_name,
            TARGET_SELECT[table_name],
            filters={"scrape_id": "is.null"},
            limit=10000,
            order=("promotion_id.asc" if table_name == "clinic_promotions" else "plan_id.asc"),
        )
        targets.extend(
            {
                **row,
                "table_name": table_name,
                "row_id": _row_id(table_name, row),
            }
            for row in rows
        )
    return targets


def fetch_raw_by_url(db: Any) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    offset = 0
    page_size = 1000
    while True:
        rows = db.fetch_rows(
            "firecrawl_scrape_raw",
            RAW_SELECT,
            limit=page_size,
            offset=offset,
            order="updated_at.desc",
        )
        for row in rows:
            url = canonical_scrape_url(str(row.get("source_url") or ""))
            if url:
                row = {**row, "source_evidence_present": True}
                grouped[url].append(row)
        if len(rows) < page_size:
            break
        offset += page_size
    return grouped


def _target_filter(table_name: str, row_id: int) -> dict[str, str]:
    key = "promotion_id" if table_name == "clinic_promotions" else "plan_id"
    return {key: f"eq.{row_id}", "scrape_id": "is.null"}


def link_targets(db: Any, targets: Iterable[dict[str, Any]], raw_id: int) -> int:
    linked = 0
    for target in targets:
        updated = db.update_row(
            target["table_name"],
            _target_filter(target["table_name"], int(target["row_id"])),
            {"scrape_id": raw_id, "updated_at": now()},
        )
        linked += len(updated)
    return linked


def scrape_url(url: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        api_url = (
            os.getenv("FIRECRAWL_SELF_HOSTED_API_URL")
            or os.getenv("FIRECRAWL_API_URL")
            or ""
        ).strip()
        api_key = str(os.getenv("FIRECRAWL_API_KEY") or "").strip()
        if not api_url:
            raise RuntimeError("Missing FIRECRAWL_SELF_HOSTED_API_URL or FIRECRAWL_API_URL")
        if not api_key:
            raise RuntimeError("Missing FIRECRAWL_API_KEY")
        response = request_v1_scrape(
            api_url,
            api_key,
            url,
            timeout_secs=float(os.getenv("FIRECRAWL_HTTP_TIMEOUT_SECS", "120")),
        )
        if not is_usable_raw({"success": True, "response_json": response}):
            return response, "firecrawl_empty_response"
        return response, None
    except Exception as exc:
        error = str(exc).strip()[:1000] or "firecrawl_scrape_failed"
        if "502" not in error and "Bad Gateway" not in error:
            return None, error
        try:
            text, metadata = scrape_public_page_text(url, timeout_seconds=20)
            return {
                "success": True,
                "data": {"markdown": text, "metadata": metadata},
                "fallback_engine": "direct_public_http",
            }, None
        except Exception as fallback_exc:
            return None, f"{error}; direct_public_http: {str(fallback_exc).strip()[:700]}"


def process_url(
    db: Any,
    url: str,
    targets: list[dict[str, Any]],
    existing_raw: dict[str, Any] | None,
    *,
    apply: bool,
) -> dict[str, Any]:
    base = {
        "source_url": url,
        "target_count": len(targets),
        "target_ids": [f"{row['table_name']}:{row['row_id']}" for row in targets],
    }
    if existing_raw is not None:
        if not apply:
            return {**base, "status": "would_link_existing", "scrape_id": int(existing_raw["id"])}
        linked = link_targets(db, targets, int(existing_raw["id"]))
        return {**base, "status": "linked_existing", "scrape_id": int(existing_raw["id"]), "linked_count": linked}

    if not apply:
        return {**base, "status": "would_scrape"}

    response, scrape_error = scrape_url(url)
    success = scrape_error is None
    raw_rows = save_scrape_response(
        db,
        scrape_request_fingerprint(url, formats=SCRAPE_FORMATS),
        url,
        response,
        success=success,
        error_message=scrape_error,
    )
    if len(raw_rows) != 1 or raw_rows[0].get("id") is None:
        raise RuntimeError("firecrawl_scrape_raw write did not return one raw id")
    raw_id = int(raw_rows[0]["id"])
    if not success:
        return {**base, "status": "scrape_failed", "scrape_id": raw_id, "error": scrape_error}
    linked = link_targets(db, targets, raw_id)
    return {**base, "status": "scraped_and_linked", "scrape_id": raw_id, "linked_count": linked}


def run(*, db: Any, apply: bool, workers: int = 3) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be positive")
    targets = fetch_targets(db)
    grouped = group_targets_by_url(targets)
    raw_by_url = fetch_raw_by_url(db)
    existing: dict[str, dict[str, Any]] = {}
    for url in grouped:
        try:
            existing[url] = choose_latest_usable_raw(raw_by_url.get(url, []))
        except ValueError:
            pass

    urls = list(grouped)
    audit: dict[str, Any] = {
        "apply": apply,
        "target_rows": len(targets),
        "unique_urls": len(urls),
        "existing_usable_raw_urls": len(existing),
        "missing_raw_urls": len(urls) - len(existing),
        "results": [],
    }
    if not apply:
        audit["results"] = [
            process_url(db, url, grouped[url], existing.get(url), apply=False) for url in urls
        ]
        return audit

    # Firecrawl calls are independent; database writes happen in process_url after each result.
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            url: executor.submit(scrape_url, url)
            for url in urls
            if url not in existing
        }
        for url in urls:
            if url in existing:
                audit["results"].append(process_url(db, url, grouped[url], existing[url], apply=True))
                continue
            response, scrape_error = futures[url].result()
            success = scrape_error is None
            raw_rows = save_scrape_response(
                db,
                scrape_request_fingerprint(url, formats=SCRAPE_FORMATS),
                url,
                response,
                success=success,
                error_message=scrape_error,
            )
            if len(raw_rows) != 1 or raw_rows[0].get("id") is None:
                raise RuntimeError(f"firecrawl_scrape_raw write did not return one raw id for {url}")
            raw_id = int(raw_rows[0]["id"])
            result = {
                "source_url": url,
                "target_count": len(grouped[url]),
                "target_ids": [f"{row['table_name']}:{row['row_id']}" for row in grouped[url]],
                "scrape_id": raw_id,
            }
            if not success:
                result.update({"status": "scrape_failed", "error": scrape_error})
            else:
                result.update({"status": "scraped_and_linked", "linked_count": link_targets(db, grouped[url], raw_id)})
            audit["results"].append(result)
    counts: dict[str, int] = defaultdict(int)
    for result in audit["results"]:
        counts[str(result["status"])] += 1
    audit["status_counts"] = dict(counts)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Call Firecrawl and write raw/link updates.")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / ".firecrawl" / "missing_scrape_links_audit.json"),
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Missing SUPABASE_URL")
    db = SupabaseRestClient(url, get_supabase_writer_key())
    with without_http_proxy():
        audit = run(db=db, apply=args.apply, workers=args.workers)
    output = Path(args.output).expanduser()
    output = output.with_name(output.name.replace(" ", "_"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
