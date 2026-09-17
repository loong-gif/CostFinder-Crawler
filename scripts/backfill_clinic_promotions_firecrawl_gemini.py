#!/usr/bin/env python3
"""Backfill missing promotion content from Firecrawl screenshots with Gemini."""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from firecrawl.v2.types import ScreenshotFormat

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.extract_clinic_promotions_from_scrape_raw import (
    llm_extract_promotions,
    merge_promotion_items,
)
from utils.firecrawl_client import get_firecrawl_cloud_client
from utils.firecrawl_scrape_raw_db import (
    canonical_scrape_url,
    save_scrape_response,
    scrape_request_fingerprint,
)
from utils.offer_extraction_llm import GeminiNativeClient
from utils.recent_raw_extraction import promotion_evidence_markdown
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key

GEMINI_MODEL = "gemini-3.5-flash-lite"
SCREENSHOT_FORMAT = ScreenshotFormat(
    type="screenshot",
    full_page=True,
    viewport={"width": 1440, "height": 1200},
    quality=80,
)
SCRAPE_FORMATS: tuple[Any, ...] = ("markdown", "links", SCREENSHOT_FORMAT)
# ponytail: fingerprint uses string tokens; SDK ScreenshotFormat is not str()-stable
FINGERPRINT_FORMATS: tuple[str, ...] = ("markdown", "links", "screenshot@fullPage")
TARGET_SELECT = "promotion_id,business_id,source_url,promotion_content,scrape_id"
LINKED_RAW_SELECT = "id,success,markdown,markdown_ocr,metadata,screenshot"
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_error(error: Any) -> str:
    return str(error or "unknown error").strip()[:1000]


def _response_data(response: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    nested = response.get("data")
    return nested if isinstance(nested, dict) else response


def _has_screenshot(response: dict[str, Any] | None) -> bool:
    return bool(str(_response_data(response).get("screenshot") or "").strip())


def select_targets(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return promotion rows that still need content, in deterministic order."""
    targets = [
        dict(row)
        for row in rows
        if isinstance(row, dict) and not list(row.get("promotion_content") or [])
    ]
    return sorted(targets, key=lambda row: int(row["promotion_id"]))


def build_gemini_client(environ: dict[str, str] | None = None) -> GeminiNativeClient:
    env = environ if environ is not None else os.environ
    api_key = str(env.get("GEMINI_API_KEY") or "").strip().strip("'\"")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY")
    return GeminiNativeClient(api_key=api_key, model=GEMINI_MODEL)


@contextmanager
def _without_http_proxy():
    """Firecrawl Cloud must bypass the project's self-hosted proxy settings."""
    saved = {key: os.environ.pop(key, None) for key in _PROXY_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def scrape_target(source_url: str) -> dict[str, Any]:
    """Fetch page evidence and a required full-page screenshot from Firecrawl."""
    with _without_http_proxy():
        client = get_firecrawl_cloud_client()
        document = client.scrape(
            source_url,
            formats=list(SCRAPE_FORMATS),
            only_main_content=True,
            block_ads=True,
            wait_for=2000,
            proxy="auto",
        )
    if hasattr(document, "model_dump"):
        return document.model_dump()
    return dict(document or {})


def _persist_raw_and_link(
    db: Any,
    *,
    target: dict[str, Any],
    response: dict[str, Any] | None,
    success: bool,
    error_message: str | None,
) -> int:
    source_url = str(target.get("source_url") or "").strip()
    fingerprint = scrape_request_fingerprint(source_url, formats=FINGERPRINT_FORMATS)
    rows = save_scrape_response(
        db,
        fingerprint,
        source_url,
        response,
        success=success,
        error_message=error_message,
    )
    if len(rows) != 1 or rows[0].get("id") is None:
        raise RuntimeError("firecrawl_scrape_raw write did not return one raw id")
    raw_id = int(rows[0]["id"])
    updated = db.update_row(
        "clinic_promotions",
        {"promotion_id": f"eq.{int(target['promotion_id'])}"},
        {"scrape_id": raw_id, "updated_at": _now()},
    )
    if len(updated) != 1:
        raise RuntimeError("clinic_promotions scrape_id update did not return one row")
    return raw_id


def process_scrape_response(
    db: Any,
    llm: Any,
    target: dict[str, Any],
    response: dict[str, Any] | None,
    scrape_error: str | None = None,
) -> dict[str, Any]:
    """Persist one raw response, link its promotion, and write validated content."""
    promotion_id = int(target["promotion_id"])
    source_url = str(target.get("source_url") or "").strip()
    if scrape_error:
        raw_id = _persist_raw_and_link(
            db,
            target=target,
            response=response,
            success=False,
            error_message=_clean_error(scrape_error),
        )
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "scrape_failed", "error": _clean_error(scrape_error)}

    if not _has_screenshot(response):
        raw_id = _persist_raw_and_link(
            db,
            target=target,
            response=response,
            success=False,
            error_message="firecrawl_screenshot_missing",
        )
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "screenshot_missing"}

    raw_id = _persist_raw_and_link(
        db,
        target=target,
        response=response,
        success=True,
        error_message=None,
    )
    evidence = promotion_evidence_markdown(_response_data(response))
    if not evidence.strip():
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "empty_markdown"}
    schema = json.loads((PROJECT_ROOT / "schema" / "promotion_extraction_schema.json").read_text(encoding="utf-8"))
    try:
        with _without_http_proxy():
            payload = llm_extract_promotions(llm, source_url=source_url, evidence=evidence, schema=schema)
        items = payload.get("promotions") or []
        if not items:
            return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "no_promotions"}
        merged = merge_promotion_items(items, evidence, source_url=source_url)
    except ValueError as exc:
        status = "no_validated_promotions" if str(exc) == "no validated promotions" else "extraction_failed"
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": status, "error": _clean_error(exc)}
    except Exception as exc:
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "gemini_failed", "error": _clean_error(exc)}

    content = list(merged.get("promotion_content") or [])
    if not content:
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "no_validated_promotions"}
    updated = db.update_row(
        "clinic_promotions",
        {"promotion_id": f"eq.{promotion_id}"},
        {"promotion_content": content, "updated_at": _now()},
    )
    if len(updated) != 1:
        raise RuntimeError("clinic_promotions content update did not return one row")
    return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "updated", "promotion_content_count": len(content)}


def process_target(db: Any, llm: Any, target: dict[str, Any]) -> dict[str, Any]:
    try:
        response = scrape_target(str(target.get("source_url") or ""))
    except Exception as exc:
        return process_scrape_response(db, llm, target, None, scrape_error=_clean_error(exc))
    return process_scrape_response(db, llm, target, response)


def process_linked_raw(db: Any, llm: Any, target: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Extract content from a Firecrawl raw row already linked to a promotion."""
    promotion_id = int(target["promotion_id"])
    source_url = str(target.get("source_url") or "").strip()
    raw_id = int(raw["id"])
    if raw.get("success") is not True:
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "linked_scrape_failed"}
    if not str(raw.get("screenshot") or "").strip():
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "linked_screenshot_missing"}
    evidence = promotion_evidence_markdown(raw)
    if not evidence.strip():
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "empty_markdown"}
    schema = json.loads((PROJECT_ROOT / "schema" / "promotion_extraction_schema.json").read_text(encoding="utf-8"))
    try:
        with _without_http_proxy():
            payload = llm_extract_promotions(llm, source_url=source_url, evidence=evidence, schema=schema)
        items = payload.get("promotions") or []
        if not items:
            return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "no_promotions"}
        merged = merge_promotion_items(items, evidence, source_url=source_url)
    except ValueError as exc:
        status = "no_validated_promotions" if str(exc) == "no validated promotions" else "extraction_failed"
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": status, "error": _clean_error(exc)}
    except Exception as exc:
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "gemini_failed", "error": _clean_error(exc)}
    content = list(merged.get("promotion_content") or [])
    if not content:
        return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "no_validated_promotions"}
    updated = db.update_row(
        "clinic_promotions",
        {"promotion_id": f"eq.{promotion_id}"},
        {"promotion_content": content, "updated_at": _now()},
    )
    if len(updated) != 1:
        raise RuntimeError("clinic_promotions content update did not return one row")
    return {"promotion_id": promotion_id, "source_url": source_url, "scrape_id": raw_id, "status": "updated", "promotion_content_count": len(content)}


def _fetch_targets(db: Any, promotion_ids: Sequence[int] | None, limit: int | None) -> list[dict[str, Any]]:
    rows = db.fetch_rows("clinic_promotions", TARGET_SELECT, limit=1000, order="promotion_id.asc")
    wanted = {int(value) for value in promotion_ids or []}
    targets = [row for row in select_targets(rows) if not wanted or int(row["promotion_id"]) in wanted]
    return targets[:limit] if limit is not None else targets


def run(
    *,
    db: Any,
    llm: Any | None = None,
    promotion_ids: Sequence[int] | None = None,
    limit: int | None = None,
    workers: int = 3,
    apply: bool = False,
    extract_linked_raw: bool = False,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be positive")
    targets = _fetch_targets(db, promotion_ids, limit)
    audit: dict[str, Any] = {"apply": apply, "targets": len(targets), "workers": workers, "results": []}
    if not apply:
        audit["results"] = [{"promotion_id": row["promotion_id"], "status": "would_process"} for row in targets]
        return audit
    if llm is None:
        llm = build_gemini_client()

    if extract_linked_raw:
        for target in targets:
            raw_id = target.get("scrape_id")
            if raw_id is None:
                audit["results"].append({"promotion_id": target["promotion_id"], "status": "missing_scrape_link"})
                continue
            rows = db.fetch_rows(
                "firecrawl_scrape_raw",
                LINKED_RAW_SELECT,
                filters={"id": f"eq.{int(raw_id)}"},
                limit=1,
            )
            if len(rows) != 1:
                audit["results"].append({"promotion_id": target["promotion_id"], "scrape_id": raw_id, "status": "missing_raw"})
                continue
            audit["results"].append(process_linked_raw(db, llm, target, rows[0]))
        statuses: dict[str, int] = {}
        for result in audit["results"]:
            status = str(result.get("status") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        audit["status_counts"] = statuses
        return audit

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(scrape_target, str(target["source_url"])) for target in targets]
        for target, future in zip(targets, futures):
            try:
                response = future.result()
                result = process_scrape_response(db, llm, target, response)
            except Exception as exc:
                try:
                    result = process_scrape_response(db, llm, target, None, scrape_error=_clean_error(exc))
                except Exception as persist_exc:
                    result = {"promotion_id": target["promotion_id"], "status": "persistence_failed", "error": _clean_error(persist_exc)}
            audit["results"].append(result)
    statuses: dict[str, int] = {}
    for result in audit["results"]:
        status = str(result.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
    audit["status_counts"] = statuses
    return audit


def _output_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path.with_name(path.name.replace(" ", "_"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Call Firecrawl and persist raw/promotion updates.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--promotion-id", action="append", type=int, default=[])
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--extract-linked-raw", action="store_true", help="Use existing linked Firecrawl raw rows; do not call Firecrawl.")
    parser.add_argument("--output", default=str(PROJECT_ROOT / ".firecrawl" / "clinic_promotions_firecrawl_gemini_backfill_audit.json"))
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Missing SUPABASE_URL")
    db = SupabaseRestClient(url, get_supabase_writer_key())
    audit = run(
        db=db,
        promotion_ids=tuple(args.promotion_id) or None,
        limit=args.limit,
        workers=args.workers,
        apply=args.apply,
        extract_linked_raw=args.extract_linked_raw,
    )
    output = _output_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
