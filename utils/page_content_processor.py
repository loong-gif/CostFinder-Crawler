"""
Shared helpers to normalize raw crawler output into staging-ready page content.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def process_page_content(content: str, source_type: str = "html") -> Dict[str, Any]:
    """Run the project's canonical cleaning and segmentation pipeline."""
    from crawler.promo_site_crawler import prepare_page_content

    return prepare_page_content(content, source_type=source_type)


def normalize_raw_page_item(
    item: Dict[str, Any],
    *,
    crawl_timestamp: Optional[str] = None,
    default_domain_name: str = "",
    default_name: Optional[str] = None,
    default_source_type: str = "markdown",
) -> Optional[Dict[str, Any]]:
    """Convert a raw crawler record into a promo_website_staging-shaped payload."""
    subpage_url = str(item.get("subpage_url") or item.get("url") or "").strip()
    raw_content = _extract_raw_content(item)
    if not subpage_url or not raw_content:
        return None

    source_type = infer_source_type(item, default=default_source_type)
    processed = process_page_content(raw_content, source_type=source_type)
    return {
        "crawl_timestamp": crawl_timestamp or datetime.now(timezone.utc).isoformat(),
        "subpage_url": subpage_url,
        "page_content": processed["page_content"],
        "page_segments_raw": _compact_json(processed["page_segments_raw"]),
        "page_segments_filtered": _compact_json(processed["page_segments_filtered"]),
        "page_content_llm": processed["page_content_llm"],
        "content_quality_flags": _compact_json(processed["content_quality_flags"]),
        "domain_name": str(item.get("domain_name") or item.get("domain") or default_domain_name).strip(),
        "processed_status": False,
        "name": (str(item.get("name") or "").strip() or default_name or None),
    }


def infer_source_type(item: Dict[str, Any], *, default: str = "markdown") -> str:
    explicit = str(
        item.get("source_type")
        or item.get("content_source_type")
        or item.get("format")
        or ""
    ).strip().lower()
    if explicit in {"html", "markdown"}:
        return explicit

    content_type = str(item.get("content_type") or "").strip().lower()
    if "html" in content_type:
        return "html"
    if "markdown" in content_type or content_type in {"md", "text/markdown"}:
        return "markdown"

    if str(item.get("html") or "").strip():
        return "html"
    if _looks_like_html(_extract_raw_content(item)):
        return "html"
    return default


def _extract_raw_content(item: Dict[str, Any]) -> str:
    for key in ("page_content", "content", "markdown", "html", "raw_content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _looks_like_html(content: str) -> bool:
    snippet = (content or "").lstrip()[:200].lower()
    return snippet.startswith("<!doctype html") or snippet.startswith("<html") or "<body" in snippet


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
