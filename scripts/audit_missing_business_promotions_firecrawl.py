#!/usr/bin/env python3
"""Audit promotion-free businesses with self-hosted Firecrawl and Gemini.

The command never writes ``clinic_promotions``.  ``--write-raw`` only permits
upserts to ``firecrawl_scrape_raw`` so every accepted audit result can point to
the page evidence that produced it.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse, urlunparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.backfill_clinic_promotions_firecrawl_gemini import build_gemini_client
from scripts.extract_clinic_promotions_from_scrape_raw import (
    llm_extract_promotions,
    merge_promotion_items,
)
from utils.firecrawl_scrape_raw_db import (
    canonical_scrape_url,
    save_scrape_response,
    scrape_request_fingerprint,
)
from utils.supabase_rest import SupabaseRestClient, get_supabase_writer_key

DEFAULT_FIRECRAWL_API_URL = "http://192.168.1.68:3002"
DEFAULT_MAP_LIMIT = 500
DEFAULT_WORKERS = 3
PAGE_SIZE = 1000
PROMOTION_URL_TOKENS = {"special", "specials", "promotion", "promotions"}
FALLBACK_PATHS = ("/special", "/specials", "/promotion", "/promotions")

_EXPLICIT_PROMOTION_PATTERNS = (
    re.compile(r"[$€£]\s*\d+(?:[,.]\d+)*"),
    re.compile(r"\b\d+(?:\.\d+)?\s*%\s*(?:off|discount)\b", re.IGNORECASE),
    re.compile(r"\b(?:off|discount(?:ed)?(?:\s+by)?)\s*\d+(?:\.\d+)?\s*%\b", re.IGNORECASE),
    re.compile(r"\bsave\s+[$€£]?\s*\d+(?:[,.]\d+)*", re.IGNORECASE),
    re.compile(r"[$€£]\s*\d+(?:[,.]\d+)*\s*off\b", re.IGNORECASE),
    re.compile(r"\bbuy\b.{0,80}\bget\b.{0,40}\bfree\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"\bfree\b.{0,80}\b(?:with|when|purchase|buy)\b", re.IGNORECASE | re.DOTALL),
)
_TEMPORAL_PATTERN = re.compile(
    r"\b(?:"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"spring|summer|fall|autumn|winter|holiday|christmas|thanksgiving|halloween|"
    r"valentine(?:'s)?|mother(?:'s)?\s+day|father(?:'s)?\s+day|labor\s+day|memorial\s+day|"
    r"today|tonight|weekend|limited[- ]time|valid|expires?|through|thru|until|ends?"
    r")\b",
    re.IGNORECASE,
)
_YEAR_TEMPORAL_PATTERN = re.compile(r"(?<![$€£])\b(?:19|20)\d{2}\b")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _normalized_host(url: Any) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _normalized_words(value: Any) -> str:
    return " ".join(_NON_WORD.sub(" ", str(value or "").casefold()).split())


def _contains_phrase(haystack: str, phrase: str) -> bool:
    return bool(phrase and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", haystack))


def _page_data(response: Mapping[str, Any]) -> dict[str, Any]:
    nested = response.get("data")
    return dict(nested) if isinstance(nested, Mapping) else dict(response)


def is_promotion_page_url(url: str) -> bool:
    """Accept only bounded special(s)/promotion(s) tokens in the URL path."""
    raw = str(url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    for segment in parsed.path.split("/"):
        tokens = set(_NON_WORD.sub(" ", unquote(segment).casefold()).split())
        if tokens & PROMOTION_URL_TOKENS:
            return True
    return False


def has_explicit_promotion_signal(text: str) -> bool:
    """Require a concrete price, discount, or purchase-bound free offer."""
    evidence = str(text or "")
    return any(pattern.search(evidence) for pattern in _EXPLICIT_PROMOTION_PATTERNS)


def activity_from_dates(
    start_date: str | None,
    end_date: str | None,
    evidence: str,
    *,
    today: date | None = None,
) -> dict[str, bool | None]:
    """Resolve audit-only activity state without inventing incomplete dates."""
    current_day = today or date.today()
    if bool(start_date) != bool(end_date):
        return {"is_active": None, "date_review_required": True}
    if start_date and end_date:
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            return {"is_active": None, "date_review_required": True}
        if start > end:
            return {"is_active": None, "date_review_required": True}
        return {
            "is_active": start <= current_day <= end,
            "date_review_required": False,
        }
    temporal_evidence = str(evidence or "")
    if _TEMPORAL_PATTERN.search(temporal_evidence) or _YEAR_TEMPORAL_PATTERN.search(
        temporal_evidence
    ):
        return {"is_active": None, "date_review_required": True}
    return {"is_active": True, "date_review_required": False}


def _website_path(url: Any) -> str:
    raw = str(url or "").strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return "/" + parsed.path.strip("/").casefold() if parsed.path.strip("/") else ""


def assign_businesses(
    page_url: str,
    evidence: str,
    businesses: Sequence[dict[str, Any]],
) -> tuple[list[int], str]:
    """Strictly attribute a page when a host has multiple business records."""
    members = [dict(row) for row in businesses]
    if len(members) == 1:
        return [int(members[0]["business_id"])], "single_host_business"
    if not members:
        return [], "unmatched_host"

    page_path = _website_path(page_url)
    path_matches = [
        int(row["business_id"])
        for row in members
        if (path := _website_path(row.get("website"))) and (
            page_path == path or page_path.startswith(f"{path}/")
        )
    ]
    if path_matches:
        return sorted(set(path_matches)), "location_path"

    blob = _normalized_words(f"{page_url} {evidence}")
    address_matches = [
        int(row["business_id"])
        for row in members
        if _contains_phrase(blob, _normalized_words(row.get("address")))
    ]
    if address_matches:
        return sorted(set(address_matches)), "address"

    normalized_names = [_normalized_words(row.get("name")) for row in members]
    name_matches = [
        int(row["business_id"])
        for row, name in zip(members, normalized_names)
        if name and normalized_names.count(name) == 1 and _contains_phrase(blob, name)
    ]
    if name_matches:
        return sorted(set(name_matches)), "business_name"

    normalized_cities = [_normalized_words(row.get("city")) for row in members]
    city_matches = [
        int(row["business_id"])
        for row, city in zip(members, normalized_cities)
        if city and normalized_cities.count(city) == 1 and _contains_phrase(blob, city)
    ]
    if city_matches:
        return sorted(set(city_matches)), "city"
    return [], "shared_host_unresolved"


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


def _empty_result(business: Mapping[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    return {
        "business_id": int(business["business_id"]),
        "name": business.get("name"),
        "website": business.get("website"),
        "host": _normalized_host(business.get("website")),
        "status": status,
        "candidate_urls": [],
        "promotions": [],
        "raw_ids": [],
        "errors": [],
        **extra,
    }


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


def _audit_host(
    *,
    db: Any,
    firecrawl: Any,
    gemini: Any,
    targets: Sequence[dict[str, Any]],
    host_members: Sequence[dict[str, Any]],
    map_limit: int,
    write_raw: bool,
    today: date,
    schema: dict[str, Any],
    raw_lock: threading.Lock,
) -> list[dict[str, Any]]:
    website = str(targets[0].get("website") or "")
    base_results = {
        int(row["business_id"]): _empty_result(row, "no_qualifying_promotion_found")
        for row in targets
    }
    try:
        mapped = firecrawl.map(_site_root(website), limit=map_limit)
    except Exception as exc:
        return [
            _empty_result(row, "map_failed", errors=[str(exc)[:1000]])
            for row in targets
        ]

    candidate_urls = _same_host_promotion_urls(website, mapped)
    if not candidate_urls:
        candidate_urls = [canonical_scrape_url(_site_root(website).rstrip("/") + path) for path in FALLBACK_PATHS]
    for result in base_results.values():
        result["candidate_urls"] = list(candidate_urls)

    matched_promotions: dict[int, list[dict[str, Any]]] = {
        int(row["business_id"]): [] for row in targets
    }
    unresolved_pages: list[dict[str, Any]] = []
    scrape_errors: list[str] = []
    gemini_errors: list[str] = []
    raw_errors: list[str] = []
    raw_ids: list[int] = []

    for source_url in candidate_urls:
        try:
            response = firecrawl.scrape(source_url)
        except Exception as exc:
            error = f"{source_url}: {str(exc)[:800]}"
            scrape_errors.append(error)
            if write_raw:
                try:
                    raw_ids.append(
                        _persist_raw(
                            db,
                            raw_lock,
                            source_url,
                            None,
                            success=False,
                            error_message=str(exc)[:1000],
                        )
                    )
                except Exception as persist_exc:
                    raw_errors.append(f"{source_url}: {str(persist_exc)[:800]}")
            continue

        raw_id: int | None = None
        if write_raw:
            try:
                raw_id = _persist_raw(
                    db,
                    raw_lock,
                    source_url,
                    response,
                    success=True,
                    error_message=None,
                )
                raw_ids.append(raw_id)
            except Exception as exc:
                raw_errors.append(f"{source_url}: {str(exc)[:800]}")
                continue

        data = _page_data(response)
        evidence = str(data.get("markdown") or "")
        metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
        page_status = metadata.get("statusCode") or metadata.get("status_code")
        if page_status is not None:
            try:
                if int(page_status) >= 400:
                    continue
            except (TypeError, ValueError):
                pass
        if not has_explicit_promotion_signal(evidence):
            continue
        try:
            extracted = llm_extract_promotions(
                gemini,
                source_url=source_url,
                evidence=evidence,
                schema=schema,
            )
            items = list(extracted.get("promotions") or [])
            if not items:
                continue
            merged = merge_promotion_items(
                items,
                evidence,
                webpage_title=str(metadata.get("title") or ""),
                source_url=source_url,
            )
        except ValueError as exc:
            if str(exc) == "no validated promotions":
                continue
            gemini_errors.append(f"{source_url}: {str(exc)[:800]}")
            continue
        except Exception as exc:
            gemini_errors.append(f"{source_url}: {str(exc)[:800]}")
            continue

        business_ids, attribution = assign_businesses(source_url, evidence, host_members)
        promotion_evidence = "\n".join(
            [
                str(merged.get("promotion_title") or ""),
                *(str(item) for item in merged.get("promotion_content") or []),
            ]
        )
        activity = activity_from_dates(
            merged.get("campaign_start_date"),
            merged.get("campaign_end_date"),
            promotion_evidence,
            today=today,
        )
        promotion = {
            "source_url": source_url,
            "raw_id": raw_id,
            "promotion_title": merged.get("promotion_title"),
            "promotion_content": list(merged.get("promotion_content") or []),
            "campaign_start_date": merged.get("campaign_start_date"),
            "campaign_end_date": merged.get("campaign_end_date"),
            "is_active": activity["is_active"],
            "attribution": attribution,
        }
        if not business_ids:
            unresolved_pages.append(promotion)
            continue
        target_ids = set(base_results)
        for business_id in business_ids:
            if business_id in target_ids:
                matched_promotions[business_id].append(dict(promotion))

    for business_id, result in base_results.items():
        result["raw_ids"] = sorted(set(raw_ids))
        result["errors"] = [*raw_errors, *scrape_errors, *gemini_errors]
        promotions = matched_promotions[business_id]
        if promotions:
            result["promotions"] = promotions
            result["status"] = (
                "date_review_required"
                if any(item.get("is_active") is None for item in promotions)
                else "promotion_found"
            )
        elif unresolved_pages:
            result["status"] = "shared_host_unresolved"
            result["unresolved_promotions"] = unresolved_pages
        elif raw_errors or scrape_errors:
            result["status"] = "scrape_failed"
        elif gemini_errors:
            result["status"] = "gemini_failed"
    return [base_results[int(row["business_id"])] for row in targets]


def audit_businesses(
    *,
    db: Any,
    firecrawl: Any,
    gemini: Any,
    targets: Sequence[dict[str, Any]],
    all_businesses: Sequence[dict[str, Any]],
    map_limit: int = DEFAULT_MAP_LIMIT,
    workers: int = 1,
    write_raw: bool = False,
    today: date | None = None,
    schema: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if map_limit < 1 or workers < 1:
        raise ValueError("map_limit and workers must be positive")
    current_day = today or date.today()
    extraction_schema = schema or json.loads(
        (PROJECT_ROOT / "schema" / "promotion_extraction_schema.json").read_text(encoding="utf-8")
    )
    results = [
        _empty_result(row, "missing_website")
        for row in targets
        if not str(row.get("website") or "").strip()
    ]
    targets_by_host: dict[str, list[dict[str, Any]]] = {}
    for row in targets:
        host = _normalized_host(row.get("website"))
        if host:
            targets_by_host.setdefault(host, []).append(dict(row))
    members_by_host: dict[str, list[dict[str, Any]]] = {}
    for row in all_businesses:
        host = _normalized_host(row.get("website"))
        if host:
            members_by_host.setdefault(host, []).append(dict(row))

    raw_lock = threading.Lock()
    jobs = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for host, host_targets in targets_by_host.items():
            jobs[
                executor.submit(
                    _audit_host,
                    db=db,
                    firecrawl=firecrawl,
                    gemini=gemini,
                    targets=host_targets,
                    host_members=members_by_host.get(host, host_targets),
                    map_limit=map_limit,
                    write_raw=write_raw,
                    today=current_day,
                    schema=extraction_schema,
                    raw_lock=raw_lock,
                )
            ] = host
        for future in as_completed(jobs):
            results.extend(future.result())
    return sorted(results, key=lambda row: int(row["business_id"]))


def _fetch_all(db: Any, table: str, select: str, *, order: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = db.fetch_rows(
            table,
            select,
            limit=PAGE_SIZE,
            offset=offset,
            order=order,
        )
        rows.extend(dict(row) for row in page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def select_missing_businesses(
    db: Any,
    *,
    business_ids: Sequence[int] | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_businesses = _fetch_all(
        db,
        "master_business_info",
        "business_id,name,website,address,city",
        order="business_id.asc",
    )
    promotions = _fetch_all(
        db,
        "clinic_promotions",
        "business_id",
        order="business_id.asc",
    )
    promoted_ids = {int(row["business_id"]) for row in promotions if row.get("business_id") is not None}
    wanted = {int(value) for value in business_ids or []}
    targets = [
        row
        for row in all_businesses
        if int(row["business_id"]) not in promoted_ids
        and (not wanted or int(row["business_id"]) in wanted)
    ]
    if limit is not None:
        targets = targets[:limit]
    return targets, all_businesses


def build_audit(results: Sequence[dict[str, Any]], *, write_raw: bool) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for row in results:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "run_date": date.today().isoformat(),
        "write_raw": write_raw,
        "businesses": len(results),
        "status_counts": status_counts,
        "balanced": sum(status_counts.values()) == len(results),
        "results": list(results),
    }


def write_reports(audit: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(audit["run_date"]).replace("-", "_")
    json_path = output_dir / f"missing_business_promotion_audit_{stamp}.json"
    csv_path = output_dir / f"missing_business_promotion_audit_{stamp}.csv"
    json_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    columns = (
        "business_id",
        "name",
        "website",
        "host",
        "status",
        "candidate_urls",
        "raw_ids",
        "promotions",
        "errors",
    )
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in audit["results"]:
            writer.writerow(
                {
                    key: (
                        json.dumps(result.get(key), ensure_ascii=False)
                        if isinstance(result.get(key), (list, dict))
                        else result.get(key)
                    )
                    for key in columns
                }
            )
    return json_path, csv_path


def write_raw_spool(db: RawSpoolDb, output_dir: Path, run_date: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(run_date).replace("-", "_")
    path = output_dir / f"missing_business_promotion_raw_{stamp}.json"
    path.write_text(
        json.dumps({"records": db.records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def reconcile_audit_raw_ids(
    audit: dict[str, Any], mapping: Mapping[int, int]
) -> dict[str, Any]:
    """Replace MCP spool IDs with the actual firecrawl_scrape_raw IDs."""
    resolved = copy.deepcopy(audit)

    def replace(promotion: dict[str, Any]) -> None:
        raw_id = promotion.get("raw_id")
        if raw_id is not None and int(raw_id) in mapping:
            promotion["raw_id"] = int(mapping[int(raw_id)])

    for result in resolved.get("results") or []:
        result["raw_ids"] = [
            int(mapping.get(int(raw_id), int(raw_id)))
            for raw_id in result.get("raw_ids") or []
        ]
        for promotion in result.get("promotions") or []:
            replace(promotion)
        for promotion in result.get("unresolved_promotions") or []:
            replace(promotion)
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--business-id", action="append", type=int, default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--map-limit", type=int, default=DEFAULT_MAP_LIMIT)
    parser.add_argument("--firecrawl-api-url", default=DEFAULT_FIRECRAWL_API_URL)
    parser.add_argument("--write-raw", action="store_true")
    parser.add_argument(
        "--mcp-stdin",
        action="store_true",
        help="Read targets/all_businesses JSON from stdin and spool raw rows for MCP import.",
    )
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / ".firecrawl"))
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.workers < 1 or args.map_limit < 1:
        parser.error("--workers and --map-limit must be positive")

    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    if args.mcp_stdin:
        payload = json.load(sys.stdin)
        all_businesses = [dict(row) for row in payload.get("all_businesses") or []]
        targets = [dict(row) for row in payload.get("targets") or []]
        wanted = {int(value) for value in args.business_id}
        if wanted:
            targets = [row for row in targets if int(row["business_id"]) in wanted]
        if args.limit is not None:
            targets = targets[: args.limit]
        db: Any = RawSpoolDb()
    else:
        supabase_url = str(os.getenv("SUPABASE_URL") or "").strip()
        if not supabase_url:
            raise RuntimeError("Missing SUPABASE_URL")
        db = SupabaseRestClient(supabase_url, get_supabase_writer_key())
    firecrawl = SelfHostedFirecrawl(
        args.firecrawl_api_url,
        str(os.getenv("FIRECRAWL_API_KEY") or "").strip(),
    )
    if not args.mcp_stdin:
        try:
            targets, all_businesses = select_missing_businesses(
                db,
                business_ids=tuple(args.business_id) or None,
                limit=args.limit,
            )
        except requests.HTTPError as exc:
            raise RuntimeError(
                "Configured SUPABASE_URL cannot read the required costfinder tables; "
                "verify that SUPABASE_URL and the writer credential belong to the same project"
            ) from exc
    firecrawl.healthcheck()
    gemini = build_gemini_client()
    results = audit_businesses(
        db=db,
        firecrawl=firecrawl,
        gemini=gemini,
        targets=targets,
        all_businesses=all_businesses,
        map_limit=args.map_limit,
        workers=args.workers,
        write_raw=args.write_raw,
    )
    audit = build_audit(results, write_raw=args.write_raw)
    output_dir = Path(args.output_dir)
    json_path, csv_path = write_reports(audit, output_dir)
    raw_spool_path = (
        write_raw_spool(db, output_dir, audit["run_date"])
        if args.mcp_stdin and args.write_raw
        else None
    )
    print(
        json.dumps(
            {
                "businesses": audit["businesses"],
                "status_counts": audit["status_counts"],
                "balanced": audit["balanced"],
                "json_output": str(json_path),
                "csv_output": str(csv_path),
                "raw_spool_output": str(raw_spool_path) if raw_spool_path else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
