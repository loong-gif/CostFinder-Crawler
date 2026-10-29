#!/usr/bin/env python3
"""Compare clinic_promotions.promotion_content with OCR text from screenshots.

This is an audit-only script. It never writes to Supabase. OCR mismatches are
reported as candidates because screenshots can contain navigation, image alt
text, or text that OCR cannot reliably read.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.schema_contract import TABLE_CLINIC_PROMOTIONS
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key


TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.,][a-z0-9]+)?", re.IGNORECASE)
NUMBER_RE = re.compile(r"(?:\$\s*)?\d+(?:[.,]\d+)?\s*%?", re.IGNORECASE)
STOPWORDS = {
    "and", "the", "for", "with", "from", "your", "you", "our", "are",
    "this", "that", "www", "http", "https", "com", "spa", "medical",
    "clinic", "special", "specials", "promotion", "promotions", "offer",
    "offers", "image", "photo", "desktop", "mobile", "new", "more",
}


def flatten_content(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(flatten_content(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(flatten_content(item) for item in value.values())
    return str(value or "")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"\[[^]]*\]\([^)]*\)", " ", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(value.lower().split())


def tokens(value: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(normalize_text(value)) if len(token) >= 3]


def meaningful_tokens(value: str) -> set[str]:
    return {token for token in tokens(value) if token not in STOPWORDS}


def numbers(value: str) -> set[str]:
    normalized = normalize_text(value).replace(",", "")
    return {re.sub(r"\s+", "", item) for item in NUMBER_RE.findall(normalized)}


def screenshot_name(url: str) -> str:
    name = Path(urlparse(url).path).name or "screenshot"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def download_and_ocr(row: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    promotion_id = int(row["promotion_id"])
    screenshot = str(row.get("screenshot") or "").strip()
    result: dict[str, Any] = {
        "promotion_id": promotion_id,
        "source_url": row.get("source_url"),
        "promotion_title": row.get("promotion_title"),
        "screenshot": screenshot,
    }
    if not screenshot:
        result["status"] = "missing_screenshot"
        return result

    image_path = output_dir / f"promotion_{promotion_id}_{screenshot_name(screenshot)}"
    try:
        response = requests.get(screenshot, timeout=45)
        response.raise_for_status()
        image_path.write_bytes(response.content)
        command = ["tesseract", str(image_path), "stdout", "--psm", "6"]
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
        result["ocr_text"] = completed.stdout
        result["status"] = "ocr_ok"
        result["image_bytes"] = len(response.content)
    except Exception as exc:  # noqa: BLE001 - audit must continue per row
        result["status"] = "ocr_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def classify(row: dict[str, Any], ocr: dict[str, Any]) -> dict[str, Any]:
    content_text = flatten_content(row.get("promotion_content"))
    result = dict(ocr)
    result["content_chars"] = len(content_text)
    result["content_items"] = len(row.get("promotion_content") or []) if isinstance(row.get("promotion_content"), list) else None
    if not content_text.strip():
        result["status"] = "missing_content"
        return result
    if ocr.get("status") != "ocr_ok":
        result["audit_status"] = "not_comparable"
        return result

    content_tokens = meaningful_tokens(content_text)
    ocr_tokens = meaningful_tokens(str(ocr.get("ocr_text") or ""))
    matched = content_tokens & ocr_tokens
    result["content_token_count"] = len(content_tokens)
    result["matched_token_count"] = len(matched)
    result["token_coverage"] = round(len(matched) / len(content_tokens), 4) if content_tokens else None
    content_numbers = numbers(content_text)
    ocr_numbers = numbers(str(ocr.get("ocr_text") or ""))
    result["content_numbers"] = sorted(content_numbers)
    result["missing_numbers"] = sorted(content_numbers - ocr_numbers)
    result["ocr_numbers"] = sorted(ocr_numbers)

    missing_items = []
    for item in row.get("promotion_content") or []:
        item_text = flatten_content(item).strip()
        item_tokens = meaningful_tokens(item_text)
        if len(item_tokens) >= 3 and len(item_tokens & ocr_tokens) / len(item_tokens) < 0.35:
            missing_items.append(item_text[:240])
    result["missing_content_items"] = missing_items[:12]

    # This is intentionally conservative: low overlap or missing numbers is a
    # review candidate, not a confirmed data error.
    if result["token_coverage"] is not None and result["token_coverage"] < 0.25:
        result["audit_status"] = "review_low_text_overlap"
    elif result["missing_numbers"]:
        result["audit_status"] = "review_missing_numbers"
    else:
        result["audit_status"] = "text_overlap_ok"
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="one-off/20260919_promotion_content_screenshot_ocr_audit.json")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    load_dotenv(".env")
    client = SupabaseRestClient(os.environ["SUPABASE_URL"], get_supabase_secret_key())
    rows = client.fetch_rows(
        TABLE_CLINIC_PROMOTIONS,
        "promotion_id,source_url,promotion_title,promotion_content,screenshot,is_active",
        limit=1000,
        order="promotion_id.asc",
    )
    comparable = [
        row for row in rows
        if row.get("screenshot") and row.get("promotion_content") not in (None, [], {})
    ]
    with tempfile.TemporaryDirectory(prefix="promotion_ocr_") as temp_dir:
        output_dir = Path(temp_dir)
        ocr_results: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {executor.submit(download_and_ocr, row, output_dir): row for row in comparable}
            for future in as_completed(futures):
                result = future.result()
                ocr_results[int(result["promotion_id"])] = result

        report = [
            classify(row, ocr_results.get(int(row["promotion_id"]), {"status": "not_processed"}))
            for row in comparable
        ]
        report_payload = {
            "engine": "tesseract-5.5.3-fallback",
            "paddleocr_status": "installed_but_cpu_inference_did_not_return_on_smoke_test",
            "total_promotions": len(rows),
            "comparable_promotions": len(comparable),
            "results": report,
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    from collections import Counter
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "total_promotions": len(rows),
        "comparable_promotions": len(comparable),
        "statuses": dict(Counter(item.get("status") for item in report)),
        "audit_statuses": dict(Counter(item.get("audit_status") for item in report)),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
