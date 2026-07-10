#!/usr/bin/env python3
"""
Use local LM Studio (schematron-3b) to extract clinic offers into schema JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import requests
from requests import ConnectionError as RequestsConnectionError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.fetch_engine import FirecrawlFetchEngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调用本地 LM Studio schematron-3b 做 offer schema 抽取")
    parser.add_argument("--url", required=True, help="目标页面 URL")
    parser.add_argument("--source-name", required=True, help="诊所/品牌名")
    parser.add_argument(
        "--schema-file",
        default=str(PROJECT_ROOT / "config" / "readerlm_offer_schema.json"),
        help="JSON Schema 文件路径",
    )
    parser.add_argument(
        "--lm-studio-url",
        default="http://127.0.0.1:1234/v1/chat/completions",
        help="LM Studio OpenAI-compatible chat completions URL",
    )
    parser.add_argument("--model", default="schematron-3b", help="LM Studio 已加载模型名")
    parser.add_argument("--api-key", default="lm-studio", help="OpenAI-compatible API key，占位即可")
    parser.add_argument("--timeout", type=int, default=180, help="请求超时秒数")
    parser.add_argument("--max-completion-tokens", type=int, default=700, help="限制模型最大输出 token 数")
    parser.add_argument(
        "--max-markdown-chars",
        type=int,
        default=18000,
        help="发送给 LM Studio 的页面 markdown 最大字符数，防止上下文超限导致 400",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "output" / "results" / "schematron_offer_extraction_result.json"),
        help="输出 JSON 路径",
    )
    return parser.parse_args()


def _extract_json(content: str) -> Dict[str, Any]:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\\s*|\\s*```$", "", raw, flags=re.DOTALL).strip()
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("模型返回内容无法解析为 JSON 对象")


def _coerce_value(value: Any, expected_type: str) -> Any:
    if expected_type == "string":
        return "" if value is None else str(value)
    if expected_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "y"}:
                return True
            if lowered in {"false", "0", "no", "n", ""}:
                return False
        return False
    if expected_type == "integer":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            cleaned = re.sub(r"[,$]", "", value).strip()
            try:
                return int(float(cleaned))
            except ValueError:
                return 0
        return 0
    if expected_type == "number":
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = re.sub(r"[,$]", "", value).strip()
            try:
                return float(cleaned)
            except ValueError:
                return 0.0
        return 0.0
    if expected_type == "object":
        return value if isinstance(value, dict) else {}
    if expected_type == "array":
        return value if isinstance(value, list) else []
    return value


def _normalize_result_with_schema(result: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    if not isinstance(properties, dict):
        return result

    normalized = dict(result)
    for key in required:
        prop = properties.get(key, {})
        expected_type = prop.get("type") if isinstance(prop, dict) else None
        if key not in normalized:
            if expected_type == "boolean":
                normalized[key] = False
            elif expected_type == "integer":
                normalized[key] = 0
            elif expected_type == "number":
                normalized[key] = 0.0
            elif expected_type == "object":
                normalized[key] = {}
            elif expected_type == "array":
                normalized[key] = []
            else:
                normalized[key] = ""
        elif isinstance(expected_type, str):
            normalized[key] = _coerce_value(normalized.get(key), expected_type)
    return normalized


def _build_extraction_schema(single_offer_schema: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["offers"],
        "properties": {
            "offers": {
                "type": "array",
                "items": single_offer_schema,
            }
        },
    }


def _extract_offer_snippets(markdown: str, max_items: int = 20) -> List[str]:
    snippets: List[str] = []
    seen: set[str] = set()
    price_or_offer = re.compile(
        r"(\$\s*\d+|\d+\s*/\s*(unit|syringe|area|ml)|\b\d+%\s*off\b|\boff\b|\bbook now\b)",
        re.IGNORECASE,
    )
    markdown_link = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")

    for text, _href in markdown_link.findall(markdown or ""):
        normalized = re.sub(r"\s+", " ", text).strip()
        if len(normalized) < 12:
            continue
        if not price_or_offer.search(normalized):
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        snippets.append(normalized)
        if len(snippets) >= max_items:
            break

    if snippets:
        return snippets

    # Fallback: line-based extraction when links are unavailable.
    for line in (markdown or "").splitlines():
        normalized = re.sub(r"\s+", " ", line).strip(" -*")
        if len(normalized) < 12:
            continue
        if not price_or_offer.search(normalized):
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        snippets.append(normalized)
        if len(snippets) >= max_items:
            break
    return snippets


def _normalize_offer_list(result: Dict[str, Any], schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    offers = result.get("offers", []) if isinstance(result, dict) else []
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        offers = []
    normalized: List[Dict[str, Any]] = []
    for item in offers:
        if isinstance(item, dict):
            normalized.append(_enrich_offer(_normalize_result_with_schema(item, schema)))
    return normalized


def _extract_price_ints(text: str) -> List[int]:
    prices: List[int] = []
    for m in re.findall(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text or ""):
        cleaned = m.replace(",", "")
        try:
            prices.append(int(round(float(cleaned))))
        except ValueError:
            continue
    return prices


def _enrich_offer(offer: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(offer)
    raw_text = str(enriched.get("offer_raw_text", "") or "")
    prices = _extract_price_ints(raw_text)

    original_price = int(enriched.get("original_price", 0) or 0)
    discount_price = int(enriched.get("discount_price", 0) or 0)
    if prices:
        if original_price <= 0:
            original_price = prices[0]
        if discount_price <= 0:
            # Most pages only expose the promo price; use detected price as discount_price fallback.
            discount_price = prices[-1]
    enriched["original_price"] = max(0, original_price)
    enriched["discount_price"] = max(0, discount_price)

    if not enriched.get("discount_amount") and enriched["original_price"] > enriched["discount_price"] > 0:
        enriched["discount_amount"] = f"${enriched['original_price'] - enriched['discount_price']}"
    return enriched


async def fetch_markdown(url: str) -> Dict[str, Any]:
    page = await FirecrawlFetchEngine().fetch(url)
    return {
        "url": page.final_url,
        "title": page.title,
        "content": page.content,
    }


def call_lm_studio(
    *,
    lm_studio_url: str,
    api_key: str,
    model: str,
    timeout: int,
    max_completion_tokens: int,
    messages: list[dict[str, str]],
) -> Dict[str, Any]:
    session = requests.Session()
    session.trust_env = False  # Ensure localhost calls are not hijacked by HTTP(S)_PROXY.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max(64, int(max_completion_tokens)),
        "messages": messages,
        "response_format": {"type": "json_object"},
    }

    response = session.post(lm_studio_url, headers=headers, json=payload, timeout=timeout)
    if response.status_code == 400:
        error_text = response.text or ""
        if "response_format" in error_text.lower() or "json_object" in error_text.lower():
            # Some local OpenAI-compatible servers/models do not support response_format.
            payload.pop("response_format", None)
            response = session.post(lm_studio_url, headers=headers, json=payload, timeout=timeout)

    if response.status_code >= 400:
        raise RuntimeError(
            f"LM Studio 请求失败: HTTP {response.status_code}.\n"
            f"Response body (first 2000 chars): {response.text[:2000]}"
        )

    payload = response.json()
    choices = payload.get("choices", [])
    if not choices:
        raise RuntimeError("LM Studio 返回缺少 choices")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
    return _extract_json(str(content))


def _build_messages(
    *,
    instruction: str,
    source_name: str,
    source_url: str,
    extraction_schema: Dict[str, Any],
    page_title: str,
    page_markdown: str,
    offer_snippets: List[str],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "You are an information extraction engine. Return strict JSON only.",
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": instruction,
                    "source_name": source_name,
                    "source_url": source_url,
                    "schema": extraction_schema,
                    "page_title": page_title,
                    "page_markdown": page_markdown,
                    "offer_snippets": offer_snippets,
                },
                ensure_ascii=False,
            ),
        },
    ]


def main() -> None:
    args = parse_args()

    schema_path = Path(args.schema_file).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    extraction_schema = _build_extraction_schema(schema)
    source_doc = asyncio.run(fetch_markdown(args.url))
    markdown = source_doc["content"] or ""
    if args.max_markdown_chars > 0 and len(markdown) > args.max_markdown_chars:
        markdown = markdown[: args.max_markdown_chars]
    offer_snippets = _extract_offer_snippets(markdown)
    focused_markdown = "\n".join(f"- {item}" for item in offer_snippets) if offer_snippets else markdown

    instruction = (
        "Extract clinic offers into strict JSON following the provided schema exactly. "
        "Return exactly one JSON object with top-level key offers (array), and no extra keys. "
        "Each element in offers must be one distinct offer item. "
        "Prefer offer_snippets over noisy navigation content from page_markdown. "
        "Use the provided source_name and source_url in every offer item. "
        "No markdown, no code fences, no repeated objects. "
        "Do not invent values. Keep required fields present. "
        "For unknown required text fields use empty string. "
        "For unknown required numeric fields use 0. "
        "For unknown required booleans use false."
    )

    messages = _build_messages(
        instruction=instruction,
        source_name=args.source_name,
        source_url=source_doc["url"],
        extraction_schema=extraction_schema,
        page_title=source_doc["title"],
        page_markdown=focused_markdown,
        offer_snippets=offer_snippets,
    )

    current_chars = len(markdown)
    while True:
        try:
            result = call_lm_studio(
                lm_studio_url=args.lm_studio_url,
                api_key=args.api_key,
                model=args.model,
                timeout=args.timeout,
                max_completion_tokens=args.max_completion_tokens,
                messages=messages,
            )
            break
        except RequestsConnectionError as exc:
            raise RuntimeError(
                f"无法连接 LM Studio: {args.lm_studio_url}。\n"
                "请确认 LM Studio 已启动本地服务器（OpenAI-compatible API）且端口正确。\n"
                "例如先检查: curl --noproxy '*' http://127.0.0.1:1234/v1/models"
            ) from exc
        except RuntimeError as exc:
            message = str(exc)
            if "n_keep" not in message or "n_ctx" not in message or current_chars <= 1500:
                raise
            current_chars = max(1500, int(current_chars * 0.7))
            markdown = markdown[:current_chars]
            messages = _build_messages(
                instruction=instruction,
                source_name=args.source_name,
                source_url=source_doc["url"],
                extraction_schema=extraction_schema,
                page_title=source_doc["title"],
                page_markdown=focused_markdown,
                offer_snippets=offer_snippets,
            )

    offers = _normalize_offer_list(result, schema)
    output_payload = {
        "meta": {
            "source_url": source_doc["url"],
            "source_title": source_doc["title"],
            "source_name": args.source_name,
            "model": args.model,
            "lm_studio_url": args.lm_studio_url,
            "offer_snippet_count": len(offer_snippets),
        },
        "offers": offers,
    }
    output_path.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
