"""URL helpers for retail / shop catalog pages."""
from __future__ import annotations

from urllib.parse import urlparse

_RETAIL_PATH_MARKERS = (
    "/collections",
    "/products/",
    "/shop",
    "/store",
)


def is_retail_catalog_url(url: str) -> bool:
    path = (urlparse((url or "").strip()).path or "/").lower()
    return any(marker in path for marker in _RETAIL_PATH_MARKERS)
