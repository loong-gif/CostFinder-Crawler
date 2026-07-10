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
    from firecrawl import Firecrawl

    load_firecrawl_env(project_root)

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing FIRECRAWL_API_KEY. Add it to .env or export it.\n"
            "Get your key at: https://firecrawl.dev/app/api-keys"
        )
    timeout = float(os.getenv("FIRECRAWL_HTTP_TIMEOUT_SECS", "120"))
    client_kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "timeout": timeout,
        "max_retries": 2,
    }
    api_url = (os.getenv("FIRECRAWL_API_URL") or "").strip()
    if api_url:
        client_kwargs["api_url"] = api_url.rstrip("/")
    return Firecrawl(**client_kwargs)
