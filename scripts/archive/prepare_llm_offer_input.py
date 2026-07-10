#!/usr/bin/env python3
"""
Prepare promo_website_staging rows for LLM offer extraction.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING
from crawler.promo_site_crawler import build_llm_ready_content, filter_page_segments
from utils.offer_extraction_llm import build_text_segments, parse_json_payload

OUTPUT_FIELDS = [
    "domain_name",
    "subpage_url",
    "page_content",
    "page_segments_raw",
    "page_segments_filtered",
    "page_content_llm",
    "content_quality_flags",
]


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def ensure_output_fields(fieldnames: Iterable[str]) -> List[str]:
    ordered = list(fieldnames)
    for field in OUTPUT_FIELDS:
        if field not in ordered:
            ordered.append(field)
    return ordered


def prepare_row(row: Dict[str, Any]) -> Dict[str, Any]:
    raw_segments = parse_json_payload(row.get("page_segments_raw"), [])
    if not isinstance(raw_segments, list) or not raw_segments:
        raw_segments = build_text_segments(row.get("page_content", ""))

    filtered_segments = parse_json_payload(row.get("page_segments_filtered"), [])
    if not isinstance(filtered_segments, list) or not filtered_segments:
        filtered_segments, content_quality_flags = filter_page_segments(raw_segments)
    else:
        content_quality_flags = parse_json_payload(row.get("content_quality_flags"), [])
        if not isinstance(content_quality_flags, list):
            content_quality_flags = []

    page_content_llm = row.get("page_content_llm", "").strip() or build_llm_ready_content(filtered_segments)
    if not page_content_llm and row.get("page_content"):
        page_content_llm = row["page_content"][:6000]
        content_quality_flags = sorted(set(content_quality_flags + ["fallback:raw_page_content"]))

    updated = dict(row)
    updated["page_segments_raw"] = compact_json(raw_segments)
    updated["page_segments_filtered"] = compact_json(filtered_segments)
    updated["page_content_llm"] = page_content_llm
    updated["content_quality_flags"] = compact_json(sorted(set(content_quality_flags)))
    return updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="准备 LLM 结构化抽取输入 CSV")
    parser.add_argument("--input", required=True, help="输入 CSV 路径")
    parser.add_argument("--output", required=True, help="输出 CSV 路径")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with input_path.open("r", newline="", encoding=OUTPUT_ENCODING) as handle:
        reader = csv.DictReader(handle)
        rows = [prepare_row(row) for row in reader]
        fieldnames = ensure_output_fields(reader.fieldnames or [])

    with output_path.open("w", newline="", encoding=OUTPUT_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
