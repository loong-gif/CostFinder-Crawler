from __future__ import annotations

import copy
import threading
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse, urlunparse

import requests

from utils.promo_paths import (
    is_promotion_url_strict as is_promotion_page_url,
    normalized_host as _normalized_host,
)
from utils.firecrawl_scrape_raw_db import (
    canonical_scrape_url,
    save_scrape_response,
    scrape_request_fingerprint,
)


def _site_root(website: str) -> str:
    raw = str(website or "").strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
    return urlunparse((scheme, parsed.netloc, "/", "", "", ""))


def _same_host_promotion_urls(website: str, urls: Iterable[Any]) -> list[str]:
    host = _normalized_host(website)
    output: list[str] = []
    seen: set[str] = set()
    for value in urls:
        if isinstance(value, Mapping):
            value = value.get("url")
        elif not isinstance(value, str):
            value = getattr(value, "url", "")
        canonical = canonical_scrape_url(str(value or ""))
        if (
            not canonical
            or _normalized_host(canonical) != host
            or not is_promotion_page_url(canonical)
            or canonical in seen
        ):
            continue
        seen.add(canonical)
        output.append(canonical)
    return output


class SelfHostedFirecrawl:
    def __init__(self, api_url: str, api_key: str = "", timeout: float = 150) -> None:
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def _session(self) -> requests.Session:
        """Return an isolated session so worker threads never share mutable state."""
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"Content-Type": "application/json"})
        if self.api_key:
            session.headers["Authorization"] = f"Bearer {self.api_key}"
        return session

    def healthcheck(self) -> None:
        response = self._session().get(
            f"{self.api_url}/health", timeout=min(self.timeout, 10)
        )
        if response.status_code >= 500:
            raise RuntimeError(f"Firecrawl health check returned HTTP {response.status_code}")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._session().post(
            f"{self.api_url}{path}", json=payload, timeout=self.timeout
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("success", True):
            raise RuntimeError(str(body.get("error") or body.get("details") or f"{path} failed"))
        return body

    def map(self, website: str, *, limit: int) -> list[Any]:
        body = self._post("/v1/map", {"url": canonical_scrape_url(website), "limit": limit})
        return list(body.get("links") or [])

    def scrape(self, url: str) -> dict[str, Any]:
        return self._post(
            "/v1/scrape",
            {
                "url": canonical_scrape_url(url),
                "formats": ["markdown", "links"],
                "onlyMainContent": True,
                "blockAds": True,
            },
        )


class RawSpoolDb:
    """Minimal raw-table sink used when Supabase access is supplied through MCP."""

    def __init__(self) -> None:
        self._records: dict[int, dict[str, Any]] = {}
        self._next_id = 1

    @property
    def records(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(self._records[key]) for key in sorted(self._records)]

    def fetch_rows(
        self,
        table: str,
        _select: str,
        *,
        filters: Mapping[str, str] | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        if table != "firecrawl_scrape_raw":
            raise RuntimeError(f"MCP spool cannot read table {table}")
        fingerprint_filter = str((filters or {}).get("request_fingerprint") or "")
        fingerprint = fingerprint_filter.removeprefix("eq.")
        return [
            {"id": row["id"]}
            for row in self._records.values()
            if row.get("request_fingerprint") == fingerprint
        ][:1]

    def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if table != "firecrawl_scrape_raw":
            raise RuntimeError(f"MCP spool cannot insert table {table}")
        inserted: list[dict[str, Any]] = []
        for payload in rows:
            row_id = self._next_id
            self._next_id += 1
            stored = {"id": row_id, **copy.deepcopy(payload)}
            self._records[row_id] = stored
            inserted.append(copy.deepcopy(stored))
        return inserted

    def update_row(
        self,
        table: str,
        filters: Mapping[str, str],
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if table != "firecrawl_scrape_raw":
            raise RuntimeError(f"MCP spool cannot update table {table}")
        raw_id = int(str(filters.get("id") or "").removeprefix("eq."))
        current = self._records.get(raw_id)
        if current is None:
            return []
        updated = {**current, **copy.deepcopy(payload)}
        self._records[raw_id] = updated
        return [copy.deepcopy(updated)]


def _persist_raw(
    db: Any,
    lock: threading.Lock,
    source_url: str,
    response: dict[str, Any] | None,
    *,
    success: bool,
    error_message: str | None,
) -> int:
    with lock:
        rows = save_scrape_response(
            db,
            scrape_request_fingerprint(source_url),
            source_url,
            response,
            success=success,
            error_message=error_message,
        )
    if len(rows) != 1 or rows[0].get("id") is None:
        raise RuntimeError("firecrawl_scrape_raw write did not return one raw id")
    return int(rows[0]["id"])
