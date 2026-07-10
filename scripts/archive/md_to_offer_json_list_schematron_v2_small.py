#!/usr/bin/env python3
"""
Convert segmented markdown into a simple offer JSON list via schematron-v2-small.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 schematron-v2-small 将分段 markdown 转为简洁 offer JSON list")
    parser.add_argument("--input-md", required=True, help="输入 markdown 文件路径（建议为 [SEGMENT n] 格式）")
    parser.add_argument("--source-url", default="", help="来源 URL（可选）")
    parser.add_argument("--source-name", default="", help="来源名称（可选）")
    parser.add_argument("--model", default="inference-net/schematron-v2-small", help="模型 ID")
    parser.add_argument("--api-url", default="https://api.inference.net/v1/chat/completions", help="API URL")
    parser.add_argument("--api-key-file", default=str(PROJECT_ROOT / "api_key.txt"), help="包含 SCHEMATRON_API_KEY 的文件路径")
    parser.add_argument("--timeout", type=int, default=180, help="请求超时秒数")
    parser.add_argument("--max-markdown-chars", type=int, default=12000, help="输入 markdown 最大字符数")
    parser.add_argument("--max-completion-tokens", type=int, default=1200, help="最大输出 token")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "output" / "results" / "schematron_offer_json_list_result.json"),
        help="输出 JSON 文件路径",
    )
    return parser.parse_args()


def load_api_key(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "SCHEMATRON_API_KEY":
            return value.strip()
    raise RuntimeError(f"在 {path} 中未找到 SCHEMATRON_API_KEY")


def extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    raise RuntimeError("模型返回内容无法解析为 JSON object")


def call_api(
    *,
    api_url: str,
    api_key: str,
    model: str,
    timeout: int,
    max_completion_tokens: int,
    messages: List[Dict[str, str]],
    response_schema: Dict[str, Any],
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max(128, int(max_completion_tokens)),
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "offer_list",
                "strict": True,
                "schema": response_schema,
            },
        },
    }
    response = requests.post(
        api_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"请求失败: HTTP {response.status_code}\n"
            f"Response body (first 1500 chars): {(response.text or '')[:1500]}"
        )
    body = response.json()
    choices = body.get("choices", [])
    if not choices:
        raise RuntimeError("API 返回缺少 choices")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return extract_json_object(str(content))


def main() -> None:
    args = parse_args()
    input_md = Path(args.input_md).expanduser().resolve()
    output_file = Path(args.output).expanduser().resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    markdown = input_md.read_text(encoding="utf-8")
    if args.max_markdown_chars > 0 and len(markdown) > args.max_markdown_chars:
        markdown = markdown[: args.max_markdown_chars]

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["offers"],
        "properties": {
            "offers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["segment", "offer"],
                    "properties": {
                        "segment": {"type": "integer"},
                        "offer": {"type": "string"},
                    },
                },
            }
        },
    }

    messages = [
        {"role": "system", "content": "You extract only offers. Return strict JSON only."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": (
                        "From the markdown, extract ONLY promotional offers. "
                        "Each offer must be one item. "
                        "segment must refer to the source [SEGMENT n] index. "
                        "Do not include navigation, footer, generic marketing copy, or standalone CTA buttons."
                    ),
                    "source_url": args.source_url,
                    "source_name": args.source_name,
                    "page_markdown": markdown,
                },
                ensure_ascii=False,
            ),
        },
    ]

    api_key = load_api_key(Path(args.api_key_file).expanduser().resolve())
    result = call_api(
        api_url=args.api_url,
        api_key=api_key,
        model=args.model,
        timeout=args.timeout,
        max_completion_tokens=args.max_completion_tokens,
        messages=messages,
        response_schema=schema,
    )

    offers = result.get("offers", []) if isinstance(result, dict) else []
    if not isinstance(offers, list):
        offers = []

    simple_list: List[Dict[str, Any]] = []
    for item in offers:
        if not isinstance(item, dict):
            continue
        segment = int(item.get("segment", 0) or 0)
        offer = str(item.get("offer", "") or "").strip()
        if not offer:
            continue
        simple_list.append({"segment": segment, "offer": offer})

    output_file.write_text(json.dumps(simple_list, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_file)


if __name__ == "__main__":
    main()

