"""Persist Firecrawl Search API responses."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from utils.schema_contract import TABLE_FIRECRAWL_SEARCH_RAW
from utils.supabase_rest import SupabaseRestClient

JsonValue = Union[dict[str, Any], list[dict[str, Any]], None]


def search_request_fingerprint(*, website: str, domain: str, query: str) -> str:
    """Stable id for one Search API call (one search_query -> one raw row)."""
    return hashlib.sha256(
        json.dumps(
            {
                "website": str(website or "").strip(),
                "domain": str(domain or "").strip(),
                "queries": [str(query or "").strip()],
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def search_web_row(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalize one Search API web hit to DB shape (no markdown)."""
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    description = str(item.get("description") or "").strip()
    if not description:
        md = str(item.get("markdown") or "").replace("\n", " ").strip()
        description = md[:240] if md else ""
    row: dict[str, Any] = {
        "url": url,
        "title": str(item.get("title") or ""),
        "description": description,
    }
    pos = item.get("position")
    if pos is not None and str(pos).strip() != "":
        row["position"] = int(pos)
    return row


def merge_search_web_rows(*groups: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    pos = 1
    for group in groups:
        for item in group:
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            out.append(
                {
                    "url": url,
                    "title": str(item.get("title") or ""),
                    "description": str(item.get("description") or ""),
                    "position": pos,
                }
            )
            pos += 1
    return out


def web_rows_from_search_payload(payload: Any) -> list[dict[str, Any]]:
    """Extract normalized web[] from Firecrawl CLI/API payload or legacy bundle object."""
    if isinstance(payload, list):
        rows = [search_web_row(item) for item in payload if isinstance(item, dict)]
        return [r for r in rows if r]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("pages"), list):
        rows = [search_web_row(item) for item in payload["pages"] if isinstance(item, dict)]
        return merge_search_web_rows([r for r in rows if r])
    web = payload.get("web")
    if isinstance(web, list):
        rows = [search_web_row(item) for item in web if isinstance(item, dict)]
        return [r for r in rows if r]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("web"), list):
        rows = [search_web_row(item) for item in data["web"] if isinstance(item, dict)]
        return [r for r in rows if r]
    return []


def web_rows_from_search_file(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return web_rows_from_search_payload(payload)


def save_search_queries(
    client: SupabaseRestClient,
    *,
    website: str,
    domain: str,
    entries: Sequence[tuple[str, list[dict[str, Any]]]],
    success: bool = True,
    error_message: Optional[str] = None,
) -> dict[str, int]:
    """Persist one firecrawl_search_raw row per search_query."""
    ids: dict[str, int] = {}
    for query, response_json in entries:
        q = str(query or "").strip()
        if not q:
            continue
        fp = search_request_fingerprint(website=website, domain=domain, query=q)
        rows = save_search_response(
            client,
            fp,
            response_json,
            search_query=q,
            success=success,
            error_message=error_message,
        )
        ids[q] = int(rows[0]["id"])
    return ids


def save_search_response(
    client: SupabaseRestClient,
    request_fingerprint: str,
    response_json: JsonValue,
    *,
    search_query: Optional[str] = None,
    response_id: Optional[str] = None,
    success: bool = True,
    error_message: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Upsert by request_fingerprint (ponytail: delete+insert, no unique constraint assumed)."""
    fp = str(request_fingerprint or "").strip()
    if not fp:
        raise ValueError("request_fingerprint is required")
    existing = client.fetch_rows(
        TABLE_FIRECRAWL_SEARCH_RAW,
        "id",
        filters={"request_fingerprint": f"eq.{fp}"},
        limit=1,
    )
    now = datetime.now(timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "request_fingerprint": fp,
        "response_json": response_json,
        "success": success,
        "error_message": error_message,
        "updated_at": now,
    }
    if search_query is not None:
        payload["search_query"] = str(search_query).strip() or None
    if response_id is not None:
        payload["response_id"] = str(response_id).strip() or None
    if existing:
        return client.update_row(
            TABLE_FIRECRAWL_SEARCH_RAW,
            {"id": f"eq.{existing[0]['id']}"},
            payload,
        )
    payload["created_at"] = now
    return client.insert_rows(TABLE_FIRECRAWL_SEARCH_RAW, [payload])


if __name__ == "__main__":
    sample = [
        {"url": "https://example.com/a", "title": "A", "description": "alpha", "position": 2},
        {"url": "https://example.com/a", "title": "A", "description": "dup"},
        {"url": "https://example.com/b", "title": "B", "markdown": "line one\nline two"},
    ]
    merged = merge_search_web_rows([r for item in sample if (r := search_web_row(item))])
    assert len(merged) == 2 and merged[0]["position"] == 1 and merged[1]["position"] == 2
    assert "markdown" not in merged[0]
    fp_a = search_request_fingerprint(
        website="example.com", domain="example.com", query="botox price site:example.com"
    )
    fp_b = search_request_fingerprint(
        website="example.com", domain="example.com", query="botox price site:example.com"
    )
    assert fp_a == fp_b and len(fp_a) == 64
