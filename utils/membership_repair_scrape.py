from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any, Dict, List, Mapping, Optional, Sequence

import requests

from utils.membership_repair_common import clean_url, page_title


def parse_rate_limit_delay(
    body: str,
    *,
    headers: Mapping[str, str] | None = None,
    default: float = 5.0,
) -> float:
    """Return the server-suggested wait before retrying a rate-limited call."""
    header_value = str((headers or {}).get("Retry-After") or "").strip()
    try:
        if header_value:
            return max(0.0, float(header_value))
    except ValueError:
        pass
    match = re.search(
        r"retry\s+(?:after|in)\s+(\d+(?:\.\d+)?)\s*s",
        str(body),
        flags=re.IGNORECASE,
    )
    if match:
        return max(0.0, float(match.group(1)))
    return max(0.0, float(default))


class FirecrawlHTTP:
    def __init__(self, base_url: str, api_key: str, *, request_interval: float = 0.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.request_interval = max(0.0, float(request_interval))
        self._request_lock = Lock()
        self._next_request_at = 0.0

    def _wait_for_request_slot(self) -> None:
        if self.request_interval <= 0:
            return
        with self._request_lock:
            now = time.monotonic()
            scheduled = max(now, self._next_request_at)
            self._next_request_at = scheduled + self.request_interval
        if scheduled > now:
            time.sleep(scheduled - now)

    def post(self, path: str, body: Dict[str, Any], timeout: int = 180) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                self._wait_for_request_slot()
                session = requests.Session()
                session.trust_env = False
                response = session.post(
                    f"{self.base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                    timeout=timeout,
                )
                if response.status_code == 429:
                    if attempt < 2:
                        time.sleep(parse_rate_limit_delay(response.text, headers=response.headers))
                        continue
                    raise RuntimeError(f"Firecrawl rate limit HTTP 429: {response.text[:300]}")
                if response.status_code in {500, 502, 503, 504}:
                    raise RuntimeError(f"Firecrawl transient HTTP {response.status_code}: {response.text[:300]}")
                response.raise_for_status()
                payload = response.json()
                if not payload.get("success", True):
                    raise RuntimeError(str(payload.get("error") or "Firecrawl request failed"))
                return payload
            except Exception as exc:  # network and endpoint errors are recorded per URL
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(str(last_error or "Firecrawl request failed"))

    def scrape(self, url: str) -> Dict[str, Any]:
        return self.post(
            "/v1/scrape",
            {
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "blockAds": True,
                "maxAge": 0,
            },
        )

    def map(self, url: str) -> Dict[str, Any]:
        return self.post(
            "/v1/map",
            {"url": url, "search": "membership club beauty bank subscription pricing", "limit": 100},
            timeout=120,
        )


def scrape_one(fc: FirecrawlHTTP, requested_url: str, fallback: bool = False) -> Dict[str, Any]:
    try:
        response = fc.scrape(requested_url)
        data = response.get("data") or {}
        metadata = data.get("metadata") or {}
        return {
            "requested_url": requested_url,
            "final_url": clean_url(metadata.get("url") or metadata.get("sourceURL") or requested_url),
            "http_status": metadata.get("statusCode"),
            "page_title": page_title(data),
            "scrape_id": metadata.get("scrapeId"),
            "cache_state": metadata.get("cacheState"),
            "fallback": fallback,
            "markdown": str(data.get("markdown") or ""),
            "error": None,
        }
    except Exception as exc:
        return {
            "requested_url": requested_url,
            "final_url": requested_url,
            "http_status": None,
            "page_title": "",
            "scrape_id": None,
            "cache_state": None,
            "fallback": fallback,
            "markdown": "",
            "error": str(exc),
        }


def scrape_many(
    fc: FirecrawlHTTP,
    urls: Sequence[str],
    *,
    fallback: bool = False,
    max_workers: int = 4,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as executor:
        futures = {executor.submit(scrape_one, fc, url, fallback): url for url in urls}
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: (item.get("requested_url") or "", item.get("fallback", False)))
