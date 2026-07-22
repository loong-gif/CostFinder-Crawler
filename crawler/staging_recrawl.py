"""
Change-gated recrawl helpers: re-crawl a domain via Firecrawl crawl API
and sync rows into promo_website_staging. Also manages promo_monitor_state with
Supabase + file fallback.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv
from utils.supabase_rest import get_supabase_writer_key
from firecrawl.v2.types import ScrapeOptions

from config.settings import FIRECRAWL_CRAWL_MAX_PAGES, FIRECRAWL_CRAWL_TIMEOUT_SECS
from crawler.promo_site_crawler import (
    SiteTarget,
    build_start_url,
    is_filtered_process_flag,
    normalize_domain)
from utils.firecrawl_client import get_firecrawl_client
from utils.logger import log
from utils.membership_paths import is_membership_page_url
from utils.url_safety import assert_safe_crawl_entry_url, crawl_entry_url_error
from utils.page_content_processor import normalize_raw_page_item
from utils.monitor_target_urls import sync_promotions_from_staging_rows

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMO_MONITOR_STATE_TABLE = "promo_monitor_state"
MONITOR_STATE_FALLBACK_PATH = PROJECT_ROOT / "output" / "monitor_results" / "monitor_state_fallback.json"

DEFAULT_MAX_CRAWL_PAGES = FIRECRAWL_CRAWL_MAX_PAGES
DEFAULT_CRAWL_TIMEOUT_SECS = FIRECRAWL_CRAWL_TIMEOUT_SECS

@dataclass(frozen=True)
class MonitorStateRow:
    monitor_id: str
    domain_name: str
    last_check_id: Optional[str] = None
    last_change_at: Optional[str] = None
    last_processed_at: Optional[str] = None

@dataclass(frozen=True)
class SyncTarget:
    domain_name: str
    website_url: str
    name: str
    master_id: Optional[int]
    business_id: Optional[int]

class SupabaseRestClient:
    """Minimal Supabase PostgREST client for staging recrawl workflows."""

    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/") + "/rest/v1"
        self.service_role_key = service_role_key
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def fetch_rows(
        self,
        table: str,
        select: str,
        *,
        filters: Optional[Dict[str, str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, str] = {"select": select}
        if filters:
            params.update(filters)
        if limit is not None:
            params["limit"] = str(limit)
        if offset is not None:
            params["offset"] = str(offset)
        if order:
            params["order"] = order
        response = self.session.get(f"{self.base_url}/{table}", params=params, timeout=60)
        response.raise_for_status()
        return response.json()

    def update_row(self, table: str, filters: Dict[str, str], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        response = self.session.patch(
            f"{self.base_url}/{table}",
            params=filters,
            headers={"Prefer": "return=representation"},
            json=payload,
            timeout=60)
        response.raise_for_status()
        return response.json()

    def insert_rows(self, table: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        response = self.session.post(
            f"{self.base_url}/{table}",
            headers={"Prefer": "return=representation"},
            json=rows,
            timeout=60)
        response.raise_for_status()
        return response.json()

    def upsert_rows(self, table: str, rows: List[Dict[str, Any]], *, on_conflict: str) -> List[Dict[str, Any]]:
        response = self.session.post(
            f"{self.base_url}/{table}",
            params={"on_conflict": on_conflict},
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            json=rows,
            timeout=60)
        response.raise_for_status()
        return response.json()

def load_supabase_client(project_root: Optional[Path] = None) -> SupabaseRestClient:
    root = project_root or Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    base_url = os.getenv("SUPABASE_URL")
    service_role_key = get_supabase_writer_key()
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return SupabaseRestClient(base_url, service_role_key)

def canonicalize_page_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = normalize_domain(parsed.netloc or parsed.path.split("/")[0])
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    clean = parsed._replace(netloc=host, path=path, query="", fragment="")
    return urlunparse(clean)

def fetch_all_rows(
    client: SupabaseRestClient,
    table: str,
    select: str,
    *,
    filters: Optional[Dict[str, str]] = None,
    page_size: int = 500,
    order: Optional[str] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.fetch_rows(
            table,
            select,
            filters=filters,
            limit=page_size,
            offset=offset,
            order=order)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return rows

def normalize_seed_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    clean = parsed._replace(query="", fragment="")
    return urlunparse(clean)

def _document_to_crawl_item(doc: Any) -> Dict[str, Any]:
    metadata = getattr(doc, "metadata", None)
    url = (getattr(metadata, "url", None) or getattr(metadata, "source_url", None) or "").strip()
    return {
        "url": url,
        "markdown": (getattr(doc, "markdown", None) or "").strip(),
        "title": (getattr(metadata, "title", None) or "").strip() if metadata else "",
    }

def _crawl_documents_to_items(documents: Iterable[Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for doc in documents:
        if isinstance(doc, dict):
            items.append(doc)
            continue
        item = _document_to_crawl_item(doc)
        if item.get("url") and item.get("markdown"):
            items.append(item)
    return items

def recrawl_domain_via_firecrawl(
    domain_name: str,
    *,
    client: Optional[SupabaseRestClient] = None,
    max_crawl_pages: int = DEFAULT_MAX_CRAWL_PAGES,
    crawl_timeout_secs: int = DEFAULT_CRAWL_TIMEOUT_SECS) -> tuple[SyncTarget, List[Dict[str, Any]], Dict[str, Any]]:
    """Re-crawl a single domain via Firecrawl crawl API."""
    sb_client = client or load_supabase_client()
    target = build_sync_target_for_domain(sb_client, domain_name)
    assert_safe_crawl_entry_url(target.website_url)
    fc = get_firecrawl_client()

    crawl_job = fc.crawl(
        target.website_url,
        limit=max_crawl_pages,
        scrape_options=ScrapeOptions(formats=["markdown"], only_main_content=True, block_ads=True),
        allow_subdomains=True,
        ignore_query_parameters=True,
        timeout=crawl_timeout_secs)
    documents = getattr(crawl_job, "data", None) or []
    crawl_rows = normalize_crawl_items(_crawl_documents_to_items(documents), target)
    log.info(
        "Firecrawl recrawl finished for {domain}: crawl_rows={rows}, status={status}".format(
            domain=target.domain_name,
            rows=len(crawl_rows),
            status=getattr(crawl_job, "status", "unknown"))
    )
    return target, crawl_rows, {
        "crawl_status": getattr(crawl_job, "status", None),
        "crawl_total": getattr(crawl_job, "total", None),
        "crawl_completed": getattr(crawl_job, "completed", None),
    }

def build_site_target_for_domain(client: SupabaseRestClient, domain_name: str) -> SiteTarget:
    normalized_domain = normalize_domain(domain_name)
    if not normalized_domain:
        raise ValueError(f"Invalid domain: {domain_name!r}")

    master_rows = fetch_all_rows(
        client,
        "master_business_info",
        "id,business_id,name,website,website_clean,process_flag",
        order="id.asc")
    promo_rows = fetch_all_rows(
        client,
        "promo_website_staging",
        "domain_name,name",
        filters={"domain_name": f"eq.{normalized_domain}"},
        order="domain_name.asc")

    master_row: Optional[Dict[str, Any]] = None
    for row in master_rows:
        row_domain = normalize_domain(row.get("website_clean") or row.get("website"))
        if row_domain != normalized_domain:
            continue
        if is_filtered_process_flag(row.get("process_flag")):
            continue
        master_row = row
        break

    promo_name = ""
    for row in promo_rows:
        promo_name = (row.get("name") or "").strip()
        if promo_name:
            break

    if master_row:
        return SiteTarget(
            master_id=master_row.get("id"),
            business_id=master_row.get("business_id"),
            name=(master_row.get("name") or promo_name or "").strip(),
            website=(master_row.get("website") or "").strip(),
            website_clean=(master_row.get("website_clean") or "").strip(),
            process_flag=(master_row.get("process_flag") or "").strip(),
            domain_name=normalized_domain)

    return SiteTarget(
        master_id=None,
        business_id=None,
        name=promo_name,
        website=f"https://{normalized_domain}",
        website_clean=normalized_domain,
        process_flag="",
        domain_name=normalized_domain)

def build_sync_target_for_domain(client: SupabaseRestClient, domain_name: str) -> SyncTarget:
    site = build_site_target_for_domain(client, domain_name)
    website_url = normalize_seed_url(build_start_url(site) or f"https://{site.domain_name}")
    if not website_url:
        raise RuntimeError(f"No crawl entry URL for domain: {site.domain_name}")
    url_error = crawl_entry_url_error(website_url)
    if url_error:
        raise ValueError(f"Unsafe crawl entry URL for {site.domain_name}: {url_error}")
    return SyncTarget(
        domain_name=site.domain_name,
        website_url=website_url,
        name=site.name,
        master_id=site.master_id,
        business_id=site.business_id)

def normalize_crawl_items(items: Iterable[Dict[str, Any]], target: SyncTarget) -> List[Dict[str, Any]]:
    crawl_timestamp = datetime.now(timezone.utc).isoformat()
    normalized: Dict[str, Dict[str, Any]] = {}
    for item in items:
        staging_row = normalize_raw_page_item(
            item,
            crawl_timestamp=crawl_timestamp,
            default_domain_name=target.domain_name,
            default_name=target.name,
            default_source_type="markdown")
        if not staging_row:
            continue
        staging_row["domain_name"] = normalize_domain(staging_row.get("domain_name") or target.domain_name) or target.domain_name
        staging_row["is_membership_page"] = is_membership_page_url(staging_row.get("subpage_url") or "")
        canonical_url = canonicalize_page_url(staging_row["subpage_url"])
        normalized[canonical_url or staging_row["subpage_url"]] = staging_row
    return list(normalized.values())

def sync_crawl_rows_to_staging(
    client: SupabaseRestClient,
    target: SyncTarget,
    crawl_rows: List[Dict[str, Any]],
    *,
    dry_run: bool = False) -> Dict[str, Any]:
    """Sync Firecrawl crawl rows into promo_website_staging (page_content diff only)."""
    existing_rows = fetch_all_rows(
        client,
        "promo_website_staging",
        "promo_website_id,domain_name,subpage_url,page_content,crawl_timestamp,processed_status,name",
        filters={"domain_name": f"eq.{target.domain_name}"},
        order="promo_website_id.asc")
    existing_by_url = {
        canonicalize_page_url(row.get("subpage_url") or ""): row
        for row in existing_rows
        if canonicalize_page_url(row.get("subpage_url") or "")
    }

    now_iso = datetime.now(timezone.utc).isoformat()
    to_update: List[Dict[str, Any]] = []
    to_insert: List[Dict[str, Any]] = []
    matched = 0
    unchanged = 0

    for row in crawl_rows:
        existing = existing_by_url.get(canonicalize_page_url(row["subpage_url"]))
        if existing is None:
            # 新行：写入首次爬取时间 crawl_timestamp，并初始化 last_updated_at。
            to_insert.append({**row, "last_updated_at": now_iso})
            continue

        matched += 1
        changed = (
            (existing.get("page_content") or "") != row["page_content"]
            or (existing.get("name") or None) != row["name"]
        )
        if not changed:
            # 内容无变更：不写库（既不动 crawl_timestamp 也不动 last_updated_at）。
            unchanged += 1
            continue

        # 内容有变更：更新内容并刷新 last_updated_at，重置 processed_status；
        # crawl_timestamp 保持首次爬取时间不变。
        to_update.append(
            {
                "promo_website_id": existing["promo_website_id"],
                "payload": {
                    "page_content": row["page_content"],
                    "name": row["name"],
                    "processed_status": False,
                    "last_updated_at": now_iso,
                    "is_membership_page": row.get("is_membership_page", False),
                },
                "subpage_url": row["subpage_url"],
            }
        )

    updated_rows = 0
    inserted_rows = 0
    if not dry_run:
        # 内容变更行逐行更新（各行 payload 不同，且只改指定列，避免误清其它列）。
        for item in to_update:
            client.update_row(
                "promo_website_staging",
                {"promo_website_id": f"eq.{item['promo_website_id']}"},
                item["payload"])
            updated_rows += 1
        if to_insert:
            client.insert_rows(
                "promo_website_staging",
                [{**row, "last_updated_at": now_iso} for row in to_insert])
            inserted_rows = len(to_insert)

    promotion_synced = sync_promotions_from_staging_rows(
        client, crawl_rows, dry_run=dry_run
    )

    return {
        "domain_name": target.domain_name,
        "website_url": target.website_url,
        "existing_rows": len(existing_rows),
        "crawl_rows": len(crawl_rows),
        "matched_rows": matched,
        "content_changed_rows": len(to_update),
        "timestamp_only_rows": unchanged,
        "insert_rows": len(to_insert),
        "updated_rows": updated_rows if not dry_run else len(to_update),
        "inserted_rows": inserted_rows if not dry_run else len(to_insert),
        "promotions_synced": promotion_synced,
        "sample_subpage_urls": [row["subpage_url"] for row in crawl_rows[:5]],
    }

def recrawl_and_sync_domain(
    domain_name: str,
    *,
    client: Optional[SupabaseRestClient] = None,
    dry_run: bool = False,
    max_crawl_pages: int = DEFAULT_MAX_CRAWL_PAGES,
    crawl_timeout_secs: int = DEFAULT_CRAWL_TIMEOUT_SECS) -> Dict[str, Any]:
    """Run Firecrawl recrawl for one domain and sync cleaned rows to promo_website_staging."""
    sb_client = client or load_supabase_client()
    target, crawl_rows, run_meta = recrawl_domain_via_firecrawl(
        domain_name,
        client=sb_client,
        max_crawl_pages=max_crawl_pages,
        crawl_timeout_secs=crawl_timeout_secs)
    sync_report = sync_crawl_rows_to_staging(sb_client, target, crawl_rows, dry_run=dry_run)
    return {
        "action": "recrawled",
        "engine": "firecrawl",
        "hit_pages": len(crawl_rows),
        "run": run_meta,
        "upsert": sync_report,
    }

def scrape_subpages_for_domain(
    domain_name: str,
    *,
    client: SupabaseRestClient,
    dry_run: bool = False,
) -> Dict[str, Any]:
    domain = normalize_domain(domain_name)
    rows = fetch_all_rows(
        client,
        "promo_website_staging",
        "subpage_url,domain_name",
        filters={"domain_name": f"eq.{domain}"},
        order="promo_website_id.asc",
    )
    urls = [r["subpage_url"] for r in rows if r.get("subpage_url")]
    if not urls:
        return recrawl_domain_via_firecrawl(domain_name, client=client, max_crawl_pages=20)

    target = build_sync_target_for_domain(client, domain)
    fc = get_firecrawl_client()
    items = []
    for url in urls:
        try:
            result = fc.scrape_url(url)
            doc = getattr(result, "data", result)
            item = _document_to_crawl_item(doc) if not isinstance(doc, dict) else doc
            if item.get("url") and item.get("markdown"):
                items.append(item)
        except Exception as exc:
            log.warning("scrape failed for {u}: {e}", u=url, e=str(exc))

    crawl_rows = normalize_crawl_items(items, target)
    sync_report = sync_crawl_rows_to_staging(client, target, crawl_rows, dry_run=dry_run)
    return {"hit_pages": len(crawl_rows), "run": {}, "upsert": sync_report}


def upsert_hits_to_staging(
    client: SupabaseRestClient,
    hits: Iterable[Dict[str, Any]],
    *,
    dry_run: bool = False) -> Dict[str, Any]:
    """Upsert crawl hits into promo_website_staging with processed_status reset on content change."""
    hit_list = list(hits)
    if not hit_list:
        return {
            "hit_rows": 0,
            "updated_rows": 0,
            "inserted_rows": 0,
            "content_changed_rows": 0,
            "timestamp_only_rows": 0,
        }

    domains = sorted({normalize_domain(row.get("domain_name")) for row in hit_list if normalize_domain(row.get("domain_name"))})
    existing_by_url: Dict[str, Dict[str, Any]] = {}
    for domain in domains:
        existing_rows = fetch_all_rows(
            client,
            "promo_website_staging",
            "promo_website_id,domain_name,subpage_url,page_content,crawl_timestamp,processed_status,name",
            filters={"domain_name": f"eq.{domain}"},
            order="promo_website_id.asc")
        for row in existing_rows:
            key = canonicalize_page_url(row.get("subpage_url") or "")
            if key:
                existing_by_url[key] = row

    to_update: List[Dict[str, Any]] = []
    to_insert: List[Dict[str, Any]] = []
    timestamp_only = 0
    content_changed = 0

    for row in hit_list:
        subpage_url = (row.get("subpage_url") or "").strip()
        if not subpage_url:
            continue
        canonical_url = canonicalize_page_url(subpage_url)
        existing = existing_by_url.get(canonical_url)

        payload = {
            "crawl_timestamp": row.get("crawl_timestamp") or datetime.now(timezone.utc).isoformat(),
            "subpage_url": subpage_url,
            "page_content": row.get("page_content") or "",
            "domain_name": normalize_domain(row.get("domain_name")) or row.get("domain_name"),
            "name": row.get("name") or None,
            "is_membership_page": is_membership_page_url(subpage_url),
        }

        if existing is None:
            to_insert.append({**payload, "processed_status": False})
            continue

        changed = (
            (existing.get("page_content") or "") != payload["page_content"]
            or (existing.get("name") or None) != payload["name"]
        )
        update_payload = dict(payload)
        if changed:
            update_payload["processed_status"] = False
            content_changed += 1
        else:
            timestamp_only += 1

        to_update.append(
            {
                "promo_website_id": existing["promo_website_id"],
                "payload": update_payload,
                "changed": changed,
            }
        )

    updated_rows = 0
    inserted_rows = 0
    if not dry_run:
        for item in to_update:
            client.update_row(
                "promo_website_staging",
                {"promo_website_id": f"eq.{item['promo_website_id']}"},
                item["payload"])
            updated_rows += 1
        if to_insert:
            client.insert_rows("promo_website_staging", to_insert)
            inserted_rows = len(to_insert)

    return {
        "hit_rows": len(hit_list),
        "updated_rows": updated_rows if not dry_run else len(to_update),
        "inserted_rows": inserted_rows if not dry_run else len(to_insert),
        "content_changed_rows": content_changed,
        "timestamp_only_rows": timestamp_only,
        "would_update_rows": len(to_update),
        "would_insert_rows": len(to_insert),
    }

class MonitorStateStore:
    """Persist monitor polling state in Supabase, with local JSON fallback if table is missing."""

    def __init__(
        self,
        client: Optional[SupabaseRestClient] = None,
        *,
        fallback_path: Path = MONITOR_STATE_FALLBACK_PATH):
        self.client = client
        self.fallback_path = fallback_path
        self.allow_fallback = os.getenv("ALLOW_MONITOR_STATE_FILE_FALLBACK", "false").strip().lower() in {"1", "true", "yes", "on"}
        self.use_supabase = client is not None
        self._fallback: Dict[str, Dict[str, Any]] = {}
        self._probe_backend()

    def _probe_backend(self) -> None:
        if not self.client:
            if not self.allow_fallback:
                raise RuntimeError("Supabase monitor state is required; set ALLOW_MONITOR_STATE_FILE_FALLBACK=true only for explicit diagnostics")
            self.use_supabase = False
            self._load_fallback()
            log.warning("Monitor state using explicitly enabled local fallback (no Supabase client).")
            return
        try:
            self.client.fetch_rows(PROMO_MONITOR_STATE_TABLE, "monitor_id", limit=1)
            self.use_supabase = True
        except Exception as exc:
            if not self.allow_fallback:
                raise RuntimeError(f"promo_monitor_state table unavailable and fallback is disabled: {exc}") from exc
            self.use_supabase = False
            self._load_fallback()
            log.error(
                "promo_monitor_state table unavailable ({error}); using explicitly enabled local fallback at {path}".format(
                    error=exc,
                    path=self.fallback_path)
            )

    def _load_fallback(self) -> None:
        if not self.fallback_path.exists():
            self._fallback = {}
            return
        try:
            payload = json.loads(self.fallback_path.read_text(encoding="utf-8"))
        except Exception:
            self._fallback = {}
            return
        rows = payload.get("rows", payload)
        if isinstance(rows, dict):
            self._fallback = rows
        else:
            self._fallback = {}

    def _save_fallback(self) -> None:
        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        self.fallback_path.write_text(
            json.dumps({"rows": self._fallback}, ensure_ascii=False, indent=2),
            encoding="utf-8")

    def get_state(self, monitor_id: str) -> Optional[MonitorStateRow]:
        if self.use_supabase and self.client:
            rows = self.client.fetch_rows(
                PROMO_MONITOR_STATE_TABLE,
                "monitor_id,domain_name,last_check_id,last_change_at,last_processed_at",
                filters={"monitor_id": f"eq.{monitor_id}"},
                limit=1)
            if rows:
                row = rows[0]
                return MonitorStateRow(
                    monitor_id=row["monitor_id"],
                    domain_name=row.get("domain_name") or "",
                    last_check_id=row.get("last_check_id"),
                    last_change_at=row.get("last_change_at"),
                    last_processed_at=row.get("last_processed_at"))
            return None

        raw = self._fallback.get(monitor_id)
        if not raw:
            return None
        return MonitorStateRow(
            monitor_id=monitor_id,
            domain_name=raw.get("domain_name") or "",
            last_check_id=raw.get("last_check_id"),
            last_change_at=raw.get("last_change_at"),
            last_processed_at=raw.get("last_processed_at"))

    def upsert_mapping(self, monitor_id: str, domain_name: str) -> None:
        """Ensure monitor_id -> domain_name mapping exists without overwriting check cursor."""
        existing = self.get_state(monitor_id)
        if existing and existing.domain_name:
            domain_name = existing.domain_name
        self.save_state(
            monitor_id=monitor_id,
            domain_name=domain_name,
            last_check_id=existing.last_check_id if existing else None,
            last_change_at=existing.last_change_at if existing else None,
            last_processed_at=existing.last_processed_at if existing else None)

    def save_state(
        self,
        *,
        monitor_id: str,
        domain_name: str,
        last_check_id: Optional[str],
        last_change_at: Optional[str] = None,
        last_processed_at: Optional[str] = None) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        payload = {
            "monitor_id": monitor_id,
            "domain_name": normalize_domain(domain_name) or domain_name,
            "last_check_id": last_check_id,
            "last_change_at": last_change_at,
            "last_processed_at": last_processed_at,
            "updated_at": now_iso,
        }

        if self.use_supabase and self.client:
            try:
                self.client.upsert_rows(
                    PROMO_MONITOR_STATE_TABLE,
                    [payload],
                    on_conflict="monitor_id")
                return
            except Exception as exc:
                if not self.allow_fallback:
                    raise RuntimeError(f"Failed to persist monitor state and fallback is disabled: {exc}") from exc
                log.error("Failed to upsert monitor state to Supabase ({error}); using explicitly enabled file fallback.", error=exc)
                self.use_supabase = False
                self._load_fallback()

        self._fallback[monitor_id] = {
            "domain_name": payload["domain_name"],
            "last_check_id": last_check_id,
            "last_change_at": last_change_at,
            "last_processed_at": last_processed_at,
            "updated_at": now_iso,
        }
        self._save_fallback()
