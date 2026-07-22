"""Shared Firecrawl SDK client factory."""
from __future__ import annotations

import base64
import os
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_firecrawl_env(project_root: Path | None = None) -> None:
    root = project_root or PROJECT_ROOT
    load_dotenv(root / ".env")


def _screenshot_api_config(project_root: Path | None = None) -> Tuple[str, str, float]:
    load_firecrawl_env(project_root)
    api_url = (
        os.getenv("FIRECRAWL_SCREENSHOT_API_URL")
        or os.getenv("FIRECRAWL_API_URL")
        or "https://api.firecrawl.dev"
    ).strip().rstrip("/")
    api_key = (
        os.getenv("FIRECRAWL_SCREENSHOT_API_KEY")
        or os.getenv("FIRECRAWL_API_KEY")
        or ""
    ).strip()
    if not api_key:
        raise RuntimeError("Missing FIRECRAWL_API_KEY for screenshot scrape")
    timeout = float(os.getenv("FIRECRAWL_HTTP_TIMEOUT_SECS", "120"))
    return api_url, api_key, timeout


def decode_screenshot_payload(value: str) -> Tuple[bytes, str]:
    """Decode Firecrawl screenshot field (URL, data URL, or raw base64) to image bytes."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("empty screenshot payload")
    if raw.startswith("data:"):
        mime_match = raw.split(";", 1)[0].split(":", 1)
        mime = mime_match[1] if len(mime_match) == 2 else "image/png"
        b64_part = raw.split("base64,", 1)[1] if "base64," in raw else raw
        return base64.b64decode(b64_part), mime
    if raw.startswith(("http://", "https://")):
        resp = requests.get(raw, timeout=60)
        resp.raise_for_status()
        mime = resp.headers.get("Content-Type", "image/png").split(";")[0].strip() or "image/png"
        return resp.content, mime
    return base64.b64decode(raw), "image/png"


def scrape_screenshot_bytes(
    page_url: str,
    *,
    project_root: Path | None = None,
    full_page: bool = True,
    wait_for_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """Capture a page screenshot via Firecrawl scrape API.

    Returns dict with image_bytes, mime, screenshot_size, warning, screenshot_ref, engine.
    Requires Fire Engine (cloud or self-hosted with screenshot support).
    """
    api_url, api_key, timeout = _screenshot_api_config(project_root)
    wait_ms = wait_for_ms if wait_for_ms is not None else int(
        os.getenv("FIRECRAWL_SCREENSHOT_WAIT_MS", "5000")
    )
    fmt = "screenshot@fullPage" if full_page else "screenshot"
    body = {
        "url": page_url,
        "formats": [fmt],
        "waitFor": wait_ms,
        "timeout": int(timeout * 1000),
    }
    resp = requests.post(
        f"{api_url}/v1/scrape",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout + 30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("success", True) and payload.get("error"):
        raise RuntimeError(str(payload.get("error")))
    data = payload.get("data") or {}
    warning = str(data.get("warning") or "").strip()
    screenshot_ref = data.get("screenshot")
    if not screenshot_ref:
        if warning and "screenshot" in warning.lower():
            raise RuntimeError(f"firecrawl_screenshot_unsupported: {warning}")
        raise RuntimeError("firecrawl_screenshot_missing")
    image_bytes, mime = decode_screenshot_payload(str(screenshot_ref))
    return {
        "image_bytes": image_bytes,
        "mime": mime,
        "screenshot_size": len(image_bytes),
        "warning": warning or None,
        "screenshot_ref": screenshot_ref if isinstance(screenshot_ref, str) and screenshot_ref.startswith("http") else None,
        "engine": "firecrawl",
    }


def get_firecrawl_client(*, project_root: Path | None = None):
    """Initialize Firecrawl client with API key and optional self-hosted URL from env."""
    return _build_firecrawl_client(
        api_key_env="FIRECRAWL_API_KEY",
        api_url_env="FIRECRAWL_API_URL",
        project_root=project_root,
    )


def get_firecrawl_search_client(*, project_root: Path | None = None):
    """Firecrawl Cloud client for search (does not inherit self-hosted FIRECRAWL_API_URL)."""
    load_firecrawl_env(project_root)
    search_key = (os.getenv("FIRECRAWL_SEARCH_API_KEY") or "").strip()
    crawl_url = (os.getenv("FIRECRAWL_API_URL") or "").strip().lower()
    if not search_key:
        if not crawl_url or "api.firecrawl.dev" in crawl_url:
            search_key = (os.getenv("FIRECRAWL_API_KEY") or "").strip()
        else:
            raise RuntimeError(
                "Firecrawl Search requires FIRECRAWL_SEARCH_API_KEY (cloud API key). "
                "Self-hosted FIRECRAWL_API_KEY cannot call search."
            )
    return _build_firecrawl_client(
        api_key_env="FIRECRAWL_SEARCH_API_KEY",
        api_url_env="FIRECRAWL_SEARCH_API_URL",
        project_root=project_root,
        default_api_url="https://api.firecrawl.dev",
        fallback_key_env="",
        api_key=search_key,
    )


def _build_firecrawl_client(
    *,
    api_key_env: str,
    api_url_env: str,
    project_root: Path | None = None,
    default_api_url: str = "",
    fallback_key_env: str = "",
    api_key: str = "",
):
    from firecrawl import Firecrawl

    load_firecrawl_env(project_root)

    if not api_key:
        api_key = (os.getenv(api_key_env) or "").strip()
    if not api_key and fallback_key_env:
        api_key = (os.getenv(fallback_key_env) or "").strip()
    if not api_key:
        raise RuntimeError(
            f"Missing {api_key_env}. Add it to .env or export it.\n"
            "Get your key at: https://firecrawl.dev/app/api-keys"
        )
    timeout = float(os.getenv("FIRECRAWL_HTTP_TIMEOUT_SECS", "120"))
    client_kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout,
        "max_retries": 2,
    }
    api_url = (os.getenv(api_url_env) or default_api_url or "").strip()
    if api_url:
        client_kwargs["api_url"] = api_url.rstrip("/")
    return Firecrawl(**client_kwargs)


def scrape_page_markdown(fc: Any, url: str) -> tuple[str, dict[str, Any]]:
    """Scrape one URL with project defaults (onlyMainContent + blockAds + denoise)."""
    from utils.firecrawl_scrape_raw_db import DEFAULT_SCRAPE_FORMATS
    from utils.scrape_markdown import prepare_scrape_markdown

    doc = fc.scrape(url, formats=list(DEFAULT_SCRAPE_FORMATS), only_main_content=True, block_ads=True)
    body = doc.model_dump() if hasattr(doc, "model_dump") else dict(doc or {})
    raw_md = str((body.get("markdown") if isinstance(body, dict) else "") or "")
    if not raw_md and isinstance(body, dict):
        data = body.get("data") or {}
        if isinstance(data, dict):
            raw_md = str(data.get("markdown") or "")
    md = prepare_scrape_markdown(raw_md)
    if isinstance(body, dict):
        body = dict(body)
        body["markdown"] = md
        data = body.get("data")
        if isinstance(data, dict):
            body["data"] = {**data, "markdown": md}
    return md, body
