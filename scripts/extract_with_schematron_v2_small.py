#!/usr/bin/env python3
"""
Use Inference.net Schematron V2 Small to process webpage text into structured JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.jina_reader_client import JinaReaderClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 Inference.net 的 schematron-v2-small 处理网页文本")
    parser.add_argument("--url", required=True, help="目标网页 URL")
    parser.add_argument("--instruction", required=True, help="抽取/处理指令")
    parser.add_argument(
        "--schema-file",
        default=str(PROJECT_ROOT / "config" / "readerlm_offer_schema.json"),
        help="可选 JSON Schema 文件路径（仅用于提示模型输出结构）",
    )
    parser.add_argument(
        "--api-base",
        default="https://api.inference.net/v1",
        help="Inference API base URL",
    )
    parser.add_argument(
        "--model",
        default="inference-net/schematron-v2-small",
        help="模型 ID",
    )
    parser.add_argument(
        "--api-key-env",
        default="INFERENCE_API_KEY",
        help="优先读取的 API Key 环境变量名",
    )
    parser.add_argument(
        "--api-key-file",
        default=str(PROJECT_ROOT / "api_key.txt"),
        help="找不到环境变量时，尝试读取的 key 文件",
    )
    parser.add_argument(
        "--api-key-name",
        default="SCHEMATRON_API_KEY",
        help="在 key 文件中优先匹配的键名",
    )
    parser.add_argument("--timeout", type=int, default=120, help="请求超时秒数")
    parser.add_argument(
        "--fetch-mode",
        choices=["jina", "direct"],
        default="jina",
        help="网页抓取模式：jina(默认) 或 direct(直连目标 URL)",
    )
    parser.add_argument("--max-completion-tokens", type=int, default=1200, help="最大输出 tokens")
    parser.add_argument(
        "--max-markdown-chars",
        type=int,
        default=30000,
        help="传给模型的网页正文最大字符数",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "output" / "results" / "schematron_v2_small_web_text_result.json"),
        help="输出 JSON 文件",
    )
    return parser.parse_args()


def _extract_json_object(content: str) -> Dict[str, Any]:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(raw[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("模型返回内容无法解析为 JSON 对象")


def _load_api_key(api_key_env: str, api_key_file: Path, api_key_name: str) -> str:
    env_key = os.getenv(api_key_env, "").strip()
    if env_key:
        return env_key

    if not api_key_file.exists():
        return ""

    raw = api_key_file.read_text(encoding="utf-8")
    lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.strip().startswith("#")]
    kv: Dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        value = v.strip().strip("'").strip('"')
        kv[key] = value

    for candidate in (api_key_name, api_key_env, "INFERENCE_API_KEY", "SCHEMATRON_API_KEY"):
        if kv.get(candidate, "").strip():
            return kv[candidate].strip()
    return ""


def _load_schema(schema_file: str) -> Dict[str, Any] | None:
    if not schema_file:
        return None
    path = Path(schema_file).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"schema 文件不存在: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("schema 文件必须是 JSON object")
    return payload


def _extract_clean_text_from_html(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    chunks: list[str] = []
    for node in soup.select("h1,h2,h3,h4,p,li,article,section"):
        text = node.get_text(" ", strip=True)
        if text:
            chunks.append(text)

    content = "\n".join(chunks).strip()
    if not content:
        content = soup.get_text(" ", strip=True)
    content = re.sub(r"\n{3,}", "\n\n", content)
    content = re.sub(r"[ \t]{2,}", " ", content)
    return content.strip()


def fetch_webpage_text(url: str, timeout: int) -> Dict[str, str]:
    session = requests.Session()
    session.trust_env = False  # Avoid unexpected local proxy interception.
    headers = {"User-Agent": "costfinder-schematron-v2-small/1.0"}
    response = session.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()

    content_type = (response.headers.get("Content-Type") or "").lower()
    if "text/html" in content_type:
        title = ""
        try:
            soup = BeautifulSoup(response.text, "lxml")
        except Exception:
            soup = BeautifulSoup(response.text, "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        content = _extract_clean_text_from_html(response.text)
    else:
        title = ""
        content = response.text.strip()

    return {
        "url": response.url.strip(),
        "title": title,
        "content": content,
    }


async def fetch_webpage_text_via_jina(url: str) -> Dict[str, str]:
    page = await JinaReaderClient().fetch(url)
    return {
        "url": page.final_url,
        "title": page.title,
        "content": page.content,
    }


def call_inference_api(
    *,
    api_base: str,
    api_key: str,
    model: str,
    timeout: int,
    max_completion_tokens: int,
    messages: list[dict[str, str]],
) -> Dict[str, Any]:
    endpoint = f"{api_base.rstrip('/')}/chat/completions"
    session = requests.Session()
    session.trust_env = False  # Avoid unexpected local proxy interception.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": max(128, int(max_completion_tokens)),
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    response = session.post(endpoint, headers=headers, json=payload, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Inference API 请求失败: HTTP {response.status_code}\n"
            f"Response body (first 2000 chars): {response.text[:2000]}"
        )

    body = response.json()
    choices = body.get("choices", [])
    if not choices:
        raise RuntimeError("API 返回缺少 choices")

    message = choices[0].get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
    return _extract_json_object(str(content))


def _build_messages(
    *,
    instruction: str,
    source_url: str,
    page_title: str,
    page_markdown: str,
    output_schema: Dict[str, Any] | None,
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a webpage text extraction engine. "
                "Always return strict JSON only, without markdown or code fences."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "instruction": instruction,
                    "source_url": source_url,
                    "page_title": page_title,
                    "page_markdown": page_markdown,
                    "output_schema": output_schema,
                    "constraints": {
                        "language": "keep source language unless instruction asks otherwise",
                        "no_hallucination": True,
                    },
                },
                ensure_ascii=False,
            ),
        },
    ]


def main() -> None:
    args = parse_args()
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    api_key = _load_api_key(
        api_key_env=args.api_key_env,
        api_key_file=Path(args.api_key_file).expanduser().resolve(),
        api_key_name=args.api_key_name,
    )
    if not api_key:
        raise RuntimeError(
            f"未找到 API Key。请设置环境变量 {args.api_key_env}，"
            f"或在 {args.api_key_file} 中配置 {args.api_key_name}=<key>"
        )

    output_schema = _load_schema(args.schema_file)
    if args.fetch_mode == "jina":
        source_doc = asyncio.run(fetch_webpage_text_via_jina(args.url))
    else:
        source_doc = fetch_webpage_text(args.url, timeout=args.timeout)
    markdown = (source_doc.get("content") or "").strip()
    if args.max_markdown_chars > 0 and len(markdown) > args.max_markdown_chars:
        markdown = markdown[: args.max_markdown_chars]

    messages = _build_messages(
        instruction=args.instruction,
        source_url=source_doc.get("url", args.url),
        page_title=source_doc.get("title", ""),
        page_markdown=markdown,
        output_schema=output_schema,
    )
    result = call_inference_api(
        api_base=args.api_base,
        api_key=api_key,
        model=args.model,
        timeout=args.timeout,
        max_completion_tokens=args.max_completion_tokens,
        messages=messages,
    )

    output_payload = {
        "meta": {
            "source_url": source_doc.get("url", args.url),
            "source_title": source_doc.get("title", ""),
            "model": args.model,
            "api_base": args.api_base,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "result": result,
    }
    output_path.write_text(json.dumps(output_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
