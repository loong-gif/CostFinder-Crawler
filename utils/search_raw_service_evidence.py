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
from utils.recent_raw_extraction import normalize_host, resolve_business


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


def group_search_rows_by_business(
    search_rows: Sequence[dict[str, Any]],
    businesses: Sequence[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """Map business_id -> search_raw rows whose response_json mentions that host."""
    by_business: dict[int, list[dict[str, Any]]] = {}
    for business in businesses:
        business_id = int(business["business_id"])
        domain = business_base_domain(business.get("website"))
        if not domain:
            continue
        matched_rows: list[dict[str, Any]] = []
        for row in search_rows:
            hits = iter_search_hits(row.get("response_json"))
            if any(host_matches_domain(str(hit.get("url") or ""), domain) for hit in hits):
                matched_rows.append(row)
        if matched_rows:
            by_business[business_id] = matched_rows
    return by_business


def resolve_business_for_website(
    website: str,
    businesses: Sequence[dict[str, Any]],
    *,
    multilocation_hosts: set[str] | None = None,
) -> int | None:
    host = normalize_host(business_base_domain(website) or website)
    if not host:
        return None
    source = {"url": f"https://{host}/", "title": "", "description": "", "text": ""}
    decision = resolve_business(
        source,
        businesses,
        multilocation_hosts or set(),
    )
    return decision.business_id if decision.accepted else None
