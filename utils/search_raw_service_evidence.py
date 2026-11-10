"""Pick service pricing evidence from firecrawl_search_raw hits."""
from __future__ import annotations

import json
from typing import Any, Sequence

from utils.clinic_services_search import (
    business_base_domain,
    host_matches_domain,
    pick_service_search_hit,
    search_hit_text,
    url_path_score,
)


def iter_search_hits(response_json: Any) -> list[dict[str, Any]]:
    if isinstance(response_json, str):
        try:
            response_json = json.loads(response_json)
        except json.JSONDecodeError:
            return []
    if not isinstance(response_json, list):
        return []
    return [hit for hit in response_json if isinstance(hit, dict)]


def collect_domain_hits(search_rows: Sequence[dict[str, Any]], *, domain: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in search_rows:
        for hit in iter_search_hits(row.get("response_json")):
            url = str(hit.get("url") or "").strip()
            if not url or url in seen or not host_matches_domain(url, domain):
                continue
            seen.add(url)
            hits.append(hit)
    return hits


def pick_service_evidence_for_business(
    search_rows: Sequence[dict[str, Any]],
    *,
    website: str,
) -> dict[str, Any] | None:
    domain = business_base_domain(website)
    if not domain:
        return None
    hits = collect_domain_hits(search_rows, domain=domain)
    picked = pick_service_search_hit(hits, domain=domain)
    if not picked:
        return None
    return {
        "source_url": str(picked.get("url") or "").strip().rstrip("/"),
        "title": str(picked.get("title") or "").strip(),
        "text": search_hit_text(picked),
        "path_score": url_path_score(str(picked.get("url") or "")),
    }
