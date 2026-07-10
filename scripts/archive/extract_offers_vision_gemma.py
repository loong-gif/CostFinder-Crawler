#!/usr/bin/env python3
"""Extract promo offers from image-based pages using Gemma 4 vision (Gemini API)."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.staging_recrawl import SupabaseRestClient
from utils.change_driven_extractor import build_offer_update_payload
from utils.vision_promo_ocr import (
    DEFAULT_GEMINI_MODEL,
    discover_promo_images,
    extract_offers_from_page,
    screenshot_extract_offers,
    vision_extract_offers_from_image,
)

OFFER_TABLE = "promo_offer_master"
OUTPUT_DIR = PROJECT_ROOT / "output" / "results"
REPORT_PREFIX = "extract_offers_vision_gemma"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gemma 4 vision extract for image-based promo pages")
    p.add_argument("--url", default=None, help="Single page URL (e.g. rmaok specials)")
    p.add_argument("--from-report", default=None, help="Classified detect report JSON")
    p.add_argument("--only-ids", default=None, help="Comma-separated promo_website_id filter")
    p.add_argument("--source-name", default=None, help="source_name for insert (default: domain from URL)")
    p.add_argument("--image-url", default=None, help="Skip discovery; OCR this image URL directly")
    p.add_argument("--model", default=DEFAULT_GEMINI_MODEL, help="Gemma 4 model id on Gemini API")
    p.add_argument("--dry-run", action="store_true", help="Extract only, do not insert")
    p.add_argument("--output-dir", default=str(OUTPUT_DIR))
    p.add_argument("--prefer-screenshot", action="store_true", help="Skip image-URL discovery; go straight to full-page screenshot OCR")
    p.add_argument("--screenshot-only", action="store_true", help="Force full-page screenshot mode (no image-URL fallback)")
    return p.parse_args()


def load_supabase_client() -> SupabaseRestClient:
    load_dotenv(PROJECT_ROOT / ".env")
    base_url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not base_url or not key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, key)


def _domain_from_url(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def build_insert_payload(offer: Dict[str, Any], *, source_url: str, source_name: str) -> Dict[str, Any]:
    payload = build_offer_update_payload(offer)
    payload.update(
        {
            "channel": "Website",
            "status": "active",
            "source_url": source_url,
            "source_name": source_name,
        }
    )
    return payload


def rows_from_args(args: argparse.Namespace) -> List[Dict[str, Any]]:
    if args.url:
        url = args.url.strip()
        return [{"subpage_url": url, "domain_name": _domain_from_url(url), "promo_website_id": None}]
    payload = json.loads(Path(args.from_report).read_text(encoding="utf-8"))
    results = payload.get("results") or []
    only_ids = None
    if args.only_ids:
        only_ids = {int(x.strip()) for x in args.only_ids.split(",") if x.strip().isdigit()}
    rows = []
    for r in results:
        if r.get("change_type") != "changed":
            continue
        if only_ids is not None and r.get("promo_website_id") not in only_ids:
            continue
        if not only_ids and r.get("unmatched_reason") != "no_offers_on_domain":
            continue
        rows.append(r)
    return rows


def process_one(
    row: Dict[str, Any],
    client: SupabaseRestClient,
    *,
    model: str,
    dry_run: bool,
    image_url_override: Optional[str] = None,
    screenshot_mode: str = "auto",  # "auto" | "prefer" | "force"
) -> Dict[str, Any]:
    url = str(row.get("subpage_url") or "").strip()
    domain = str(row.get("domain_name") or _domain_from_url(url))
    result: Dict[str, Any] = {
        "promo_website_id": row.get("promo_website_id"),
        "subpage_url": url,
        "domain_name": domain,
        "image_urls": [],
        "extracted_offers": 0,
        "inserted": 0,
        "insert_errors": [],
        "offers_preview": [],
        "screenshot_mode": screenshot_mode,
    }

    if screenshot_mode == "force":
        try:
            vision = screenshot_extract_offers(url, model=model, project_root=PROJECT_ROOT)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"screenshot_ocr: {exc}"
            return result
        all_offers = vision.get("offers") or []
        result["screenshot_size"] = vision.get("screenshot_size")
        if vision.get("screenshot_engine"):
            result["screenshot_engine"] = vision.get("screenshot_engine")
        if vision.get("firecrawl_error"):
            result["firecrawl_error"] = vision.get("firecrawl_error")
        if vision.get("error") and not result.get("error"):
            result["error"] = vision.get("error")
    elif image_url_override:
        images = [image_url_override.strip()]
        result["image_urls"] = images
        all_offers = []
        for img_url in images:
            try:
                vision = vision_extract_offers_from_image(img_url, page_url=url, model=model)
                all_offers.extend(vision.get("offers") or [])
            except Exception as exc:  # noqa: BLE001
                result.setdefault("vision_errors", []).append(f"{img_url}: {exc}")
    else:
        try:
            combined = extract_offers_from_page(
                url, model=model, project_root=PROJECT_ROOT,
                prefer_screenshot=(screenshot_mode == "prefer"),
            )
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"extract_offers_from_page: {exc}"
            return result
        all_offers = combined.get("offers") or []
        result["image_urls"] = combined.get("image_urls", []) or []
        if "screenshot_size" in combined:
            result["screenshot_size"] = combined["screenshot_size"]

    result["extracted_offers"] = len(all_offers)
    result["offers_preview"] = [
        {"service_name": o.get("service_name"), "offer_raw_text": str(o.get("offer_raw_text") or "")[:120]}
        for o in all_offers[:8]
    ]

    for offer in all_offers:
        payload = build_insert_payload(offer, source_url=url, source_name=domain)
        if not payload.get("service_name") and not payload.get("offer_raw_text"):
            continue
        if dry_run:
            result["inserted"] += 1
            continue
        try:
            client.insert_rows(OFFER_TABLE, [payload])
            result["inserted"] += 1
        except Exception as exc:  # noqa: BLE001
            result["insert_errors"].append(str(exc))
    return result


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    rows = rows_from_args(args)
    if not rows:
        print(json.dumps({"status": "no_rows"}, ensure_ascii=False))
        return

    client = load_supabase_client()
    screenshot_mode = "force" if args.screenshot_only else "prefer" if args.prefer_screenshot else "auto"
    results = [
        process_one(r, client, model=args.model, dry_run=args.dry_run, image_url_override=args.image_url, screenshot_mode=screenshot_mode)
        for r in rows
    ]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"{REPORT_PREFIX}_{ts}.json"
    csv_path = out_dir / f"{REPORT_PREFIX}_{ts}.csv"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "dry_run" if args.dry_run else "apply",
        "model": args.model,
        "results": results,
        "summary": {
            "total": len(results),
            "extracted_offers_total": sum(int(r.get("extracted_offers") or 0) for r in results),
            "inserted_total": sum(int(r.get("inserted") or 0) for r in results),
        },
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["promo_website_id", "subpage_url", "extracted_offers", "inserted", "image_urls", "error"],
            extrasaction="ignore",
        )
        w.writeheader()
        for r in results:
            row = dict(r)
            row["image_urls"] = ";".join(r.get("image_urls") or [])
            w.writerow(row)

    print(json.dumps({"status": payload["mode"], "summary": payload["summary"], "json_path": str(json_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
