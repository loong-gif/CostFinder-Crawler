"""Persist Firecrawl Scrape API responses (one row per URL)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse, urlunparse

from utils.schema_contract import TABLE_FIRECRAWL_SCRAPE_RAW
from utils.scrape_markdown import prepare_scrape_markdown
from utils.supabase_rest import SupabaseRestClient

DEFAULT_SCRAPE_FORMATS: tuple[str, ...] = ("markdown", "links")


def canonical_scrape_url(url: str) -> str:
    """Normalize URL for fingerprint/source_url (scheme/host lower, no fragment)."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunparse((parsed.scheme.lower() or "https", host, path, "", "", ""))


def scrape_request_fingerprint(
    url: str,
    *,
    formats: Sequence[str] | None = None,
    only_main_content: bool = True,
    block_ads: bool = True,
) -> str:
    """Stable key for upsert; excludes search query so same page reuses cache."""
    canonical = canonical_scrape_url(url)
    if not canonical:
        raise ValueError("url is required")
    fmt = sorted(f.strip() for f in (formats or DEFAULT_SCRAPE_FORMATS) if str(f).strip())
    payload = {
        "url": canonical,
        "formats": fmt,
        "only_main_content": only_main_content,
        "block_ads": block_ads,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def scrape_response_to_row_fields(response: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Map Firecrawl scrape API/CLI payload to table columns."""
    if not response:
        return {}
    body = response if isinstance(response, dict) else {}
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        return {}

    credits_raw = body.get("creditsUsed")
    credits_used: Optional[int] = None
    if credits_raw is not None and str(credits_raw).strip() != "":
        credits_used = int(credits_raw)

    markdown = data.get("markdown")
    if markdown is not None:
        markdown = prepare_scrape_markdown(str(markdown))

    row: Dict[str, Any] = {
        "markdown": markdown,
        "html": data.get("html"),
        "raw_html": data.get("rawHtml") or data.get("raw_html"),
        "links": data.get("links"),
        "metadata": data.get("metadata"),
        "screenshot": data.get("screenshot"),
        "warning": data.get("warning") or body.get("warning"),
        "scrape_job_id": body.get("id"),
        "credits_used": credits_used,
    }
    images = data.get("images")
    if isinstance(images, list):
        row["images"] = [str(item) for item in images if str(item).strip()]
    return {k: v for k, v in row.items() if v is not None}


def save_scrape_response(
    client: SupabaseRestClient,
    request_fingerprint: str,
    source_url: str,
    response_json: Optional[Dict[str, Any]],
    *,
    search_raw_id: Optional[int] = None,
    success: bool = True,
    error_message: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Upsert by request_fingerprint (unique constraint on table)."""
    fp = str(request_fingerprint or "").strip()
    if not fp:
        raise ValueError("request_fingerprint is required")
    norm_url = canonical_scrape_url(source_url)
    if not norm_url:
        raise ValueError("source_url is required")
    existing = client.fetch_rows(
        TABLE_FIRECRAWL_SCRAPE_RAW,
        "id",
        filters={"request_fingerprint": f"eq.{fp}"},
        limit=1,
    )
    now = datetime.now(timezone.utc).isoformat()
    payload: Dict[str, Any] = {
        "request_fingerprint": fp,
        "source_url": norm_url,
        "success": success,
        "error_message": error_message,
        "updated_at": now,
    }
    payload.update(scrape_response_to_row_fields(response_json))
    if search_raw_id is not None:
        payload["search_raw_id"] = search_raw_id
    if existing:
        return client.update_row(
            TABLE_FIRECRAWL_SCRAPE_RAW,
            {"id": f"eq.{existing[0]['id']}"},
            payload,
        )
    payload["created_at"] = now
    return client.insert_rows(TABLE_FIRECRAWL_SCRAPE_RAW, [payload])
