#!/usr/bin/env python3
"""
Build a small labeled evaluation set for offer extraction QA.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING


def classify_bucket(row: Dict[str, str]) -> str:
    text = f"{row.get('subpage_url', '')} {row.get('page_content_llm', '')} {row.get('page_content', '')}".lower()
    flags = (row.get("content_quality_flags") or "").lower()
    if any(term in text for term in ["membership", "memberships", "monthly", "special", "promo", "promotion"]):
        return "membership_promotions"
    if any(term in text for term in ["shop", "view product", "cart", "product"]) and "service" not in text:
        return "product_list"
    if any(term in text for term in ["service", "services", "pricing", "price list", "menu", "facial", "botox", "filler"]):
        return "service_menu"
    if "noise" in flags or len((row.get("page_content_llm") or "").strip()) < 80:
        return "noise"
    return "other"


def sample_rows(rows: List[Dict[str, str]], sample_size: int) -> List[Dict[str, str]]:
    bucket_targets = {
        "product_list": 8,
        "service_menu": 8,
        "membership_promotions": 8,
        "noise": 6,
    }
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(classify_bucket(row), []).append(row)

    selected: List[Dict[str, str]] = []
    seen_keys = set()

    for bucket, target in bucket_targets.items():
        for row in grouped.get(bucket, [])[:target]:
            key = (row.get("domain_name", ""), row.get("subpage_url", ""))
            if key in seen_keys:
                continue
            selected.append({**row, "eval_bucket": bucket})
            seen_keys.add(key)

    if len(selected) < sample_size:
        for bucket in ["other", "service_menu", "membership_promotions", "product_list", "noise"]:
            for row in grouped.get(bucket, []):
                key = (row.get("domain_name", ""), row.get("subpage_url", ""))
                if key in seen_keys:
                    continue
                selected.append({**row, "eval_bucket": classify_bucket(row)})
                seen_keys.add(key)
                if len(selected) >= sample_size:
                    break
            if len(selected) >= sample_size:
                break

    return selected[:sample_size]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建抽取评估集")
    parser.add_argument("--input", required=True, help="准备后的 CSV 路径")
    parser.add_argument("--output", required=True, help="评估集 CSV 路径")
    parser.add_argument("--sample-size", type=int, default=30, help="样本数")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with input_path.open("r", newline="", encoding=OUTPUT_ENCODING) as handle:
        rows = list(csv.DictReader(handle))

    sampled = sample_rows(rows, args.sample_size)
    fieldnames = [
        "eval_bucket",
        "annotation_status",
        "should_extract_expected",
        "expected_offer_count",
        "expected_key_fields",
        "review_notes",
    ]
    source_fields = list(rows[0].keys()) if rows else []
    for field in source_fields:
        if field not in fieldnames:
            fieldnames.append(field)

    with output_path.open("w", newline="", encoding=OUTPUT_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in sampled:
            writer.writerow(
                {
                    "eval_bucket": row.get("eval_bucket", ""),
                    "annotation_status": "todo",
                    "should_extract_expected": "",
                    "expected_offer_count": "",
                    "expected_key_fields": "",
                    "review_notes": "",
                    **row,
                }
            )


if __name__ == "__main__":
    main()
