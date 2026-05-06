"""
Jina Reader API 客户端
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config.settings import (
    JINA_READER_API_KEY,
    JINA_READER_BASE_URL,
    JINA_READER_INSTRUCTION,
    JINA_READER_NO_CACHE,
    JINA_READER_JSON_SCHEMA,
    JINA_READER_RESPOND_WITH,
    JINA_READER_TIMEOUT,
    JINA_READER_USE_JSON_MODE,
    JINA_READER_WITH_GENERATED_ALT,
)

_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BARE_URL_PATTERN = re.compile(r"https?://[^\s)\]>\"]+")
_TITLE_PATTERN = re.compile(r"^Title:\s*(.+)$", re.MULTILINE)
_URL_SOURCE_PATTERN = re.compile(r"^URL Source:\s*(.+)$", re.MULTILINE)
_MARKDOWN_CONTENT_PATTERN = re.compile(r"^Markdown Content:\s*(.*)$", re.DOTALL | re.MULTILINE)


@dataclass(frozen=True)
class ReaderPage:
    """Reader 返回的结构化页面数据"""

    request_url: str
    final_url: str
    title: str
    content: str
    links: List[Dict[str, str]]


class JinaReaderClient:
    """异步封装 `https://r.jina.ai`"""

    def __init__(self):
        self.base_url = JINA_READER_BASE_URL.rstrip("/")
        self.timeout = max(1, int(JINA_READER_TIMEOUT))
        self.respond_with = JINA_READER_RESPOND_WITH
        self.api_key = JINA_READER_API_KEY
        self.json_schema = self._normalize_json_schema(JINA_READER_JSON_SCHEMA)
        self.instruction = JINA_READER_INSTRUCTION
        self.allow_direct_fallback = os.getenv("JINA_READER_ALLOW_DIRECT_FALLBACK", "true").lower() in {"1", "true", "yes"}
        self.verify_ssl = os.getenv("JINA_READER_VERIFY_SSL", "true").lower() in {"1", "true", "yes"}
        self.session = requests.Session()
        # Ignore shell proxy env vars to avoid local proxy hijacking in automation.
        self.session.trust_env = False

    async def fetch(self, target_url: str) -> ReaderPage:
        reader_url = self._build_reader_url(target_url)
        headers = self._build_headers()
        try:
            response = await asyncio.to_thread(
                self.session.get,
                reader_url,
                headers=headers,
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
        except requests.RequestException:
            if not self.allow_direct_fallback:
                raise
            return await self._fetch_direct_page(target_url)

        final_url = target_url
        title = ""
        content = ""

        if JINA_READER_USE_JSON_MODE:
            try:
                payload = response.json()
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                data_payload = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                final_url = (data_payload.get("url") or target_url).strip()
                title = (data_payload.get("title") or "").strip()
                content = (data_payload.get("content") or "").strip()

        if not content:
            final_url, title, content = self._parse_text_response(response.text, fallback_url=target_url)

        links = self._extract_links(content, final_url)
        return ReaderPage(
            request_url=target_url,
            final_url=final_url,
            title=title,
            content=content,
            links=links,
        )

    async def _fetch_direct_page(self, target_url: str) -> ReaderPage:
        headers = {"User-Agent": "costfinder-direct-fetch/1.0"}
        response = await asyncio.to_thread(
            self.session.get,
            target_url,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")
        title = (soup.title.get_text(strip=True) if soup.title else "").strip()
        for tag_name in ("script", "style", "noscript"):
            for node in soup.find_all(tag_name):
                node.decompose()
        content = soup.get_text("\n", strip=True)

        links: Dict[str, Dict[str, str]] = {}
        for a in soup.find_all("a", href=True):
            href = urljoin(target_url, a.get("href", "").strip())
            if not href.startswith(("http://", "https://")):
                continue
            links[href] = {"href": href, "text": a.get_text(strip=True)}

        return ReaderPage(
            request_url=target_url,
            final_url=response.url,
            title=title,
            content=content,
            links=list(links.values()),
        )

    def _build_reader_url(self, target_url: str) -> str:
        safe_target_url = target_url.strip()
        return f"{self.base_url}/{safe_target_url}"

    def _build_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"User-Agent": "costfinder-jina-reader/1.0"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if JINA_READER_USE_JSON_MODE:
            headers["Accept"] = "application/json"
        if self.respond_with:
            headers["x-respond-with"] = self.respond_with
        if self.json_schema:
            headers["x-json-schema"] = self.json_schema
        if self.instruction:
            headers["x-instruction"] = self.instruction
        if JINA_READER_NO_CACHE:
            headers["x-no-cache"] = "true"
        if JINA_READER_WITH_GENERATED_ALT:
            headers["x-with-generated-alt"] = "true"
        return headers

    def _normalize_json_schema(self, raw_schema: str) -> str:
        schema = (raw_schema or "").strip()
        if not schema:
            return ""
        try:
            parsed = json.loads(schema)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JINA_READER_JSON_SCHEMA 不是合法 JSON: {exc}") from exc
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))

    def _parse_text_response(self, raw_text: str, *, fallback_url: str) -> tuple[str, str, str]:
        title_match = _TITLE_PATTERN.search(raw_text)
        source_match = _URL_SOURCE_PATTERN.search(raw_text)
        markdown_match = _MARKDOWN_CONTENT_PATTERN.search(raw_text)

        title = (title_match.group(1) if title_match else "").strip()
        final_url = (source_match.group(1) if source_match else fallback_url).strip()
        if markdown_match:
            content = markdown_match.group(1).strip()
        else:
            content = raw_text.strip()

        return final_url, title, content

    def _extract_links(self, markdown_text: str, base_url: str) -> List[Dict[str, str]]:
        links: Dict[str, Dict[str, str]] = {}

        for anchor_text, href in _MARKDOWN_LINK_PATTERN.findall(markdown_text):
            absolute_url = urljoin(base_url, href.strip())
            if not absolute_url.startswith(("http://", "https://")):
                continue
            links[absolute_url] = {
                "href": absolute_url,
                "text": anchor_text.strip(),
            }

        for href in _BARE_URL_PATTERN.findall(markdown_text):
            normalized_href = href.strip()
            if normalized_href not in links:
                links[normalized_href] = {"href": normalized_href, "text": ""}

        return list(links.values())
