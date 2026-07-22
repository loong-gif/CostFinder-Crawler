"""SSRF URL trust boundary for outbound crawl entry URLs."""
from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlparse


def crawl_entry_url_error(url: str) -> Optional[str]:
    """Return a rejection reason, or None when the URL is safe to crawl."""
    raw = (url or "").strip()
    if not raw:
        return "empty url"

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return f"unsupported scheme: {scheme or '(none)'}"

    if parsed.username or parsed.password:
        return "userinfo not allowed"

    host = (parsed.hostname or "").strip()
    if not host:
        return "empty host"

    host_lower = host.lower().rstrip(".")
    if host_lower == "localhost":
        return "localhost not allowed"
    if host_lower == "local" or host_lower.endswith(".local"):
        return ".local host not allowed"

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None

    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return f"blocked ip: {host}"

    return None


def assert_safe_crawl_entry_url(url: str) -> str:
    """Return the URL when safe; raise ValueError with reason otherwise."""
    reason = crawl_entry_url_error(url)
    if reason:
        raise ValueError(reason)
    return (url or "").strip()
