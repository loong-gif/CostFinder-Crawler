"""Shared Firecrawl SDK client factory."""
from __future__ import annotations

import os
from typing import Any, Dict

from dotenv import load_dotenv
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_firecrawl_env(project_root: Path | None = None) -> None:
    root = project_root or PROJECT_ROOT
    load_dotenv(root / ".env")


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
