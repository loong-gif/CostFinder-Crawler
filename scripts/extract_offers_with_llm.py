#!/usr/bin/env python3
"""
Two-stage LLM extraction for promo offer rows.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import OUTPUT_ENCODING
from utils.offer_extraction_llm import build_client_from_env, extract_offers_for_row


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="两阶段 LLM 结构化抽取")
    parser.add_argument("--input", required=True, help="输入 CSV 路径")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 行")
    parser.add_argument("--api-url", default=None, help="OpenAI-compatible chat completions URL")
    parser.add_argument("--model", default=None, help="模型名称")
    parser.add_argument("--api-key-env", default="LLM_API_KEY", help="读取 API Key 的环境变量")
    parser.add_argument("--dry-run", action="store_true", help="仅输出两阶段 prompts 与启发式选择结果")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    client = None if args.dry_run else build_client_from_env(api_url=args.api_url, model=args.model, api_key_env=args.api_key_env)

    with input_path.open("r", newline="", encoding=OUTPUT_ENCODING) as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)

    if args.limit:
        rows = rows[: args.limit]

    payload: List[Dict[str, Any]] = []
    for row in rows:
        result = extract_offers_for_row(row, client=client)
        payload.append(result)

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
