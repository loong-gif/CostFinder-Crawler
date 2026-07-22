#!/usr/bin/env python3
"""Mark is_ocr_required, re-scrape with images, fill markdown_ocr via PaddleOCR."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from utils.firecrawl_client import get_firecrawl_client
from utils.firecrawl_scrape_raw_db import scrape_response_to_row_fields
from utils.paddle_ocr_markdown import build_markdown_ocr, filter_promo_image_urls, image_urls_from_markdown
from utils.schema_contract import TABLE_FIRECRAWL_SCRAPE_RAW
from utils.scrape_markdown import prepare_scrape_markdown
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key


def _doc_to_payload(doc: Any) -> dict[str, Any]:
    if hasattr(doc, "model_dump"):
        return doc.model_dump()
    return dict(doc or {})


def _extract_images(body: dict[str, Any]) -> list[str]:
    images = body.get("images")
    if isinstance(images, list):
        return filter_promo_image_urls([str(item) for item in images if str(item).strip()])
    data = body.get("data")
    if isinstance(data, dict) and isinstance(data.get("images"), list):
        return filter_promo_image_urls([str(item) for item in data["images"] if str(item).strip()])
    markdown = str(body.get("markdown") or (data or {}).get("markdown") or "")
    return image_urls_from_markdown(markdown)


def rescrape_one(client: Any, row: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    scrape_id = int(row["id"])
    source_url = str(row["source_url"])
    fc = get_firecrawl_client()
    doc = fc.scrape(
        source_url,
        formats=["markdown", "links", "images"],
        only_main_content=True,
        block_ads=True,
        wait_for=3000,
    )
    body = _doc_to_payload(doc)
    fields = scrape_response_to_row_fields({"success": True, "data": body, **body})
    markdown = prepare_scrape_markdown(str(fields.get("markdown") or row.get("markdown") or ""))
    image_urls = _extract_images({**body, "markdown": markdown})
    markdown_ocr, ocr_meta = build_markdown_ocr(image_urls) if image_urls else ("", [])
    audit = {
        "id": scrape_id,
        "source_url": source_url,
        "is_ocr_required": True,
        "image_count": len(image_urls),
        "markdown_ocr_len": len(markdown_ocr),
        "ocr_meta": ocr_meta,
        "applied": False,
    }
    if not apply:
        audit["preview_ocr"] = markdown_ocr[:2000]
        return audit
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "is_ocr_required": True,
        "markdown": markdown,
        "markdown_ocr": markdown_ocr or None,
        "updated_at": now,
    }
    if image_urls:
        payload["images"] = image_urls
    for key in ("html", "raw_html", "links", "metadata", "screenshot", "warning", "scrape_job_id", "credits_used"):
        if fields.get(key) is not None:
            payload[key] = fields[key]
    client.update_row(TABLE_FIRECRAWL_SCRAPE_RAW, {"id": f"eq.{scrape_id}"}, payload)
    audit["applied"] = True
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", required=True, help="comma-separated firecrawl_scrape_raw.id values")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    import os

    base_url = os.getenv("SUPABASE_URL", "").strip()
    client = SupabaseRestClient(base_url, get_supabase_secret_key())
    ids = ",".join(part.strip() for part in args.ids.split(",") if part.strip())
    rows = client.fetch_rows(
        TABLE_FIRECRAWL_SCRAPE_RAW,
        "id,source_url,markdown,images,is_ocr_required",
        filters={"id": f"in.({ids})"},
        limit=len(ids.split(",")),
    )
    audits = [rescrape_one(client, row, apply=args.apply) for row in sorted(rows, key=lambda r: int(r["id"]))]
    out_path = PROJECT_ROOT / ".firecrawl/master-business-search/rescrape-ocr-audit.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"apply": args.apply, "results": audits}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audits, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
