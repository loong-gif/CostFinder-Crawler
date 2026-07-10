#!/usr/bin/env python3
"""
Filter rows whose caption contains price-related information.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING
from utils.caption_price_filter import extract_price_signals

DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "results" / "caption_price_filtered.csv"
EXTRA_FIELDS = ["matched_price_signals", "matched_price_signal_labels", "matched_price_signal_count"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 caption 字段中过滤包含价格信息的数据")
    parser.add_argument("--input", required=True, help="输入文件路径，支持 CSV / JSON / JSONL")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出 CSV 路径")
    parser.add_argument("--caption-field", default="caption", help="caption 字段名，默认是 caption")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 条记录")
    return parser.parse_args()


def load_csv_rows(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    with path.open("r", newline="", encoding=OUTPUT_ENCODING) as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return rows, list(reader.fieldnames or [])


def load_json_rows(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            rows = payload["items"]
        else:
            rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError("JSON 输入必须是对象、对象数组，或带 items 数组的对象")

    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("JSON 输入中的每一项都必须是对象")

    fieldnames = collect_fieldnames(rows)
    return rows, fieldnames


def load_jsonl_rows(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"JSONL 第 {line_number} 行不是对象")
            rows.append(item)

    return rows, collect_fieldnames(rows)


def collect_fieldnames(rows: Sequence[Dict[str, Any]]) -> List[str]:
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    return fieldnames


def load_rows(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return load_csv_rows(path)
    if suffix == ".json":
        return load_json_rows(path)
    if suffix == ".jsonl":
        return load_jsonl_rows(path)
    raise ValueError("仅支持 .csv / .json / .jsonl 输入")


def ensure_output_fields(fieldnames: Iterable[str]) -> List[str]:
    ordered = list(fieldnames)
    for field in EXTRA_FIELDS:
        if field not in ordered:
            ordered.append(field)
    return ordered


def stringify_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def filter_rows(rows: Sequence[Dict[str, Any]], caption_field: str) -> List[Dict[str, Any]]:
    matched_rows: List[Dict[str, Any]] = []

    for row in rows:
        caption = stringify_value(row.get(caption_field, ""))
        signals = extract_price_signals(caption)
        if not signals:
            continue

        updated = dict(row)
        updated["matched_price_signals"] = json.dumps([signal.match_text for signal in signals], ensure_ascii=False)
        updated["matched_price_signal_labels"] = json.dumps([signal.label for signal in signals], ensure_ascii=False)
        updated["matched_price_signal_count"] = str(len(signals))
        matched_rows.append(updated)

    return matched_rows


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding=OUTPUT_ENCODING) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    rows, fieldnames = load_rows(input_path)
    if args.limit is not None:
        rows = rows[: args.limit]

    matched_rows = filter_rows(rows, caption_field=args.caption_field)
    output_fields = ensure_output_fields(fieldnames)
    write_csv(output_path, matched_rows, output_fields)

    summary = {
        "input_path": str(input_path),
        "output_path": str(output_path),
        "total_rows": len(rows),
        "matched_rows": len(matched_rows),
        "caption_field": args.caption_field,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
