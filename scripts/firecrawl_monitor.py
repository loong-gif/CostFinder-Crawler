#!/usr/bin/env python3
"""
Firecrawl Monitor integration for promo_website_staging.

Monitors medspa/beauty business websites for price/promotion changes.
Uses Firecrawl's monitoring API with JSON-mode change tracking to detect
structured field changes (prices, offers, service names).

Usage:
    # Create monitors for all domains in promo_website_staging
    python scripts/firecrawl_monitor.py create-all

    # Create monitor for a single domain
    python scripts/firecrawl_monitor.py create --domain example-medspa.com

    # Retarget existing monitors to promo/pricing subpages from staging
    python scripts/firecrawl_monitor.py sync-targets --dry-run

    # List all monitors
    python scripts/firecrawl_monitor.py list

    # Get check results for a monitor
    python scripts/firecrawl_monitor.py checks --monitor-id <id>

    # Get check detail with page diffs
    python scripts/firecrawl_monitor.py check-detail --monitor-id <id> --check-id <id>

    # Delete a monitor
    python scripts/firecrawl_monitor.py delete --monitor-id <id>

    # Run a monitor immediately
    python scripts/firecrawl_monitor.py run --monitor-id <id>

Environment:
    FIRECRAWL_API_KEY   - Required. Your Firecrawl API key.
    SUPABASE_URL        - Required for create-all. Supabase project URL.
    SUPABASE_SERVICE_ROLE_KEY - Required for create-all. Supabase service role key.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from utils.supabase_rest import get_supabase_writer_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.promo_site_crawler import normalize_domain
from crawler.staging_recrawl import PROMO_MONITOR_STATE_TABLE, MonitorStateStore, load_supabase_client
from utils.firecrawl_client import get_firecrawl_client
from utils.monitor_target_urls import (
    fetch_monitor_urls_from_promotions,
    normalize_monitor_url,
    resolve_monitor_subpage_urls,
)
from utils.service_category_lookup import MASTER_CATEGORY_PROMPT

OUTPUT_DIR = PROJECT_ROOT / "output" / "monitor_results"
REPORT_PREFIX = "firecrawl_monitor"


def load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def get_supabase_client():
    """Initialize Supabase REST client."""
    import requests

    base_url = os.getenv("SUPABASE_URL")
    service_role_key = get_supabase_writer_key()
    if not base_url or not service_role_key:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")

    session = requests.Session()
    session.headers.update({
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return session, f"{base_url.rstrip('/')}/rest/v1"


def save_monitor_mapping(monitor_id: str, domain_name: str) -> None:
    """Persist monitor_id -> domain_name mapping for polling workflows."""
    if not monitor_id or monitor_id == "unknown":
        return
    try:
        state_store = MonitorStateStore(load_supabase_client(PROJECT_ROOT))
        state_store.upsert_mapping(monitor_id, domain_name)
    except Exception as exc:
        print(f"  Warning: failed to save monitor mapping for {domain_name}: {exc}")


def reset_monitor_baseline(state_store: MonitorStateStore, monitor_id: str, domain_name: str) -> None:
    """Clear poll cursor after retargeting so the next poll re-baselines."""
    state_store.save_state(
        monitor_id=monitor_id,
        domain_name=domain_name,
        last_check_id=None,
        last_change_at=None,
        last_processed_at=None,
    )


# ---------------------------------------------------------------------------
# Schema for structured change tracking (prices, offers, services)
# ---------------------------------------------------------------------------

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "business_name": {"type": "string"},
        "offers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "service_category": {"type": "string"},
                    "regular_price": {"type": "number"},
                    "discount_price": {"type": "number"},
                    "unit_type": {"type": "string"},
                    "template_type": {"type": "string"},
                    "valid_from": {"type": "string"},
                    "valid_through": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
    },
}

EXTRACTION_PROMPT = (
    "Extract each service offer, special, and promotion as a separate item in the offers array. "
    "For per-unit pricing (Botox, fillers, neurotoxins), capture the numeric price AND unit_type "
    "(unit, syringe, area, vial). For promotions with was/now pricing, capture both regular_price "
    "(the original/was price) and discount_price (the sale/now price). "
    "Membership plan tiers (monthly fee, tier name, benefits) are tracked separately — do not "
    "include them in offers. Do not extract free consultations or consultation-only bookings. "
    "Do not extract retail skincare/catalog shop SKUs from /collections or /shop pages as treatment offers. "
    "encode plan billing_period on individual service items. "
    f"Set service_category to one of: {MASTER_CATEGORY_PROMPT}. "
    "Return one item per distinct price point, not one per category."
)


def _retry_firecrawl(label: str, func, *, retries: int = 5, delay: int = 2):
    for attempt in range(retries):
        try:
            return func()
        except Exception as exc:
            err_msg = str(exc)
            retryable = (
                "rate limit" in err_msg.lower()
                or "too many requests" in err_msg.lower()
                or "429" in err_msg
                or "timeout" in err_msg.lower()
                or "timed out" in err_msg.lower()
            )
            if retryable:
                retry_match = re.search(r"retry after (\d+)s", err_msg)
                wait = int(retry_match.group(1)) + 2 if retry_match else delay * (2 ** attempt)
                reason = "rate limited" if "429" in err_msg or "rate limit" in err_msg.lower() else "transient error"
                print(f"  {label}: {reason}, waiting {wait}s...", flush=True)
                time.sleep(wait)
                continue
            if attempt == retries - 1:
                raise
            time.sleep(delay)
    raise RuntimeError(f"{label}: failed after {retries} retries")


def build_scrape_target(urls: List[str]) -> Dict[str, Any]:
    clean_urls = [normalize_monitor_url(u) for u in urls if u]
    if not clean_urls:
        raise ValueError("At least one monitor URL is required")
    return {
        "type": "scrape",
        "urls": clean_urls,
        "scrapeOptions": {
            "formats": [
                {
                    "type": "changeTracking",
                    "modes": ["json", "git-diff"],
                    "prompt": EXTRACTION_PROMPT,
                    "schema": EXTRACTION_SCHEMA,
                }
            ],
            "maxAge": 0,
            "onlyMainContent": True,
            "blockAds": True,
        },
    }


# ---------------------------------------------------------------------------
# Monitor creation
# ---------------------------------------------------------------------------

def create_monitor_for_domain(
    client,
    domain_name: str,
    urls: List[str],
    name: str = "",
    webhook_url: Optional[str] = None,
    email_recipients: Optional[List[str]] = None,
    schedule_text: str = "daily",
    timezone: str = "America/Phoenix",
    goal: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Firecrawl monitor for a single domain."""

    monitor_name = name or f"Promo monitor: {domain_name}"

    if not goal:
        goal = (
            f"Detect changes to pricing, special offers, promotions, "
            f"membership plans, and service prices on {domain_name}. "
            f"Alert when any price changes, new offers appear, or existing "
            f"offers are removed or modified."
        )

    monitor = client.create_monitor(
        name=monitor_name,
        schedule={"text": schedule_text, "timezone": timezone},
        goal=goal,
        targets=[build_scrape_target(urls)],
        notification=(
            {
                "email": {
                    "enabled": True,
                    "recipients": email_recipients,
                    "includeDiffs": True,
                }
            }
            if email_recipients
            else None
        ),
        webhook=(
            {
                "url": webhook_url,
                "events": ["monitor.page", "monitor.check.completed"],
            }
            if webhook_url
            else None
        ),
    )

    return monitor


def monitor_url_source() -> str:
    return (os.getenv("MONITOR_URL_SOURCE") or "both").strip().lower()


def fetch_promotion_urls_by_domain(client) -> Dict[str, List[str]]:
    rows = client.fetch_rows(
        "clinic_promotions",
        "source_url,business_id,master_business_info(website)",
        limit=5000,
        order="promotion_id.asc",
    )
    out: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        url = str(row.get("source_url") or "").strip()
        if not url:
            continue
        domain = normalize_domain(url)
        if domain:
            out[domain].append(url)
    return dict(out)


def resolve_domain_monitor_urls(
    *,
    domain: str,
    staging_urls: List[str],
    promotion_urls: List[str],
    max_urls: int,
) -> List[str]:
    return resolve_monitor_subpage_urls(
        promotion_urls=promotion_urls,
        staging_urls=staging_urls,
        domain_name=domain,
        source=monitor_url_source(),
        max_urls=max_urls,
    )


def fetch_staging_urls_by_domain(session, base_url: str) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Return domain -> subpage_urls and domain -> business name."""
    print("Fetching subpage_url rows from promo_website_staging...")
    resp = session.get(
        f"{base_url}/promo_website_staging",
        params={
            "select": "domain_name,subpage_url,name",
            "domain_name": "not.is.null",
            "subpage_url": "not.is.null",
            "order": "domain_name.asc",
        },
        timeout=120,
    )
    resp.raise_for_status()
    rows = resp.json()

    urls_by_domain: Dict[str, List[str]] = defaultdict(list)
    names_by_domain: Dict[str, str] = {}
    for row in rows:
        domain = normalize_domain(row.get("domain_name") or "")
        url = (row.get("subpage_url") or "").strip()
        if not domain or not url:
            continue
        urls_by_domain[domain].append(url)
        if domain not in names_by_domain:
            names_by_domain[domain] = (row.get("name") or "").strip()
    return dict(urls_by_domain), names_by_domain


def create_monitors_for_all_domains(args: argparse.Namespace) -> None:
    """Create monitors for all domains in promo_website_staging."""
    load_env()
    fc = get_firecrawl_client()
    session, base_url = get_supabase_client()
    urls_by_domain, names_by_domain = fetch_staging_urls_by_domain(session, base_url)
    sb_client = load_supabase_client()
    promo_by_domain = fetch_promotion_urls_by_domain(sb_client)

    domains = dict(names_by_domain)
    if args.limit:
        domains = dict(list(domains.items())[: args.limit])

    if args.domain:
        d = normalize_domain(args.domain)
        if d in domains:
            domains = {d: domains[d]}
        else:
            print(f"Domain '{d}' not found in promo_website_staging")
            return

    print("Checking existing monitors...")
    existing_names = {m.get("name", "") for m in list_all_monitors(fc)}

    new_domains: Dict[str, str] = {}
    for d, n in domains.items():
        monitor_name = n or f"Promo monitor: {d}"
        if monitor_name not in existing_names:
            new_domains[d] = n

    skipped = len(domains) - len(new_domains)
    if skipped:
        print(f"Skipping {skipped} domains with existing monitors.")
    domains = new_domains

    if not domains:
        print("All domains already have monitors.")
        return

    print(f"Creating monitors for {len(domains)} new domains...")
    email_recipients = [e.strip() for e in args.email.split(",")] if args.email else None
    webhook_url = args.webhook
    max_urls = max(1, args.max_urls)

    results: List[Dict[str, Any]] = []
    errors = 0

    for i, (domain, name) in enumerate(domains.items(), 1):
        target_urls = resolve_domain_monitor_urls(
            domain=domain,
            staging_urls=urls_by_domain.get(domain, []),
            promotion_urls=promo_by_domain.get(domain, []),
            max_urls=max_urls,
        )
        try:
            monitor = _create_with_rate_limit_retry(
                fc,
                domain=domain,
                name=name,
                urls=target_urls,
                webhook_url=webhook_url,
                email_recipients=email_recipients,
                schedule_text=args.schedule,
                timezone=args.timezone,
            )
            md = _obj_to_dict(monitor.data if hasattr(monitor, "data") else monitor)
            mid = md.get("id", "unknown")
            print(f"  [{i}/{len(domains)}] Created: {domain} -> {mid}  urls={target_urls}")
            save_monitor_mapping(mid, domain)
            results.append({"domain": domain, "monitor_id": mid, "urls": target_urls, "status": "created"})
        except Exception as exc:
            print(f"  [{i}/{len(domains)}] ERROR: {domain} -> {exc}")
            results.append({"domain": domain, "monitor_id": None, "status": "error", "error": str(exc)[:200]})
            errors += 1

    _write_report("create", {"total": len(domains), "created": len([r for r in results if r["status"] == "created"]), "errors": errors, "results": results})


def _create_with_rate_limit_retry(
    fc,
    *,
    domain: str,
    name: str,
    urls: List[str],
    webhook_url: Optional[str],
    email_recipients: Optional[List[str]],
    schedule_text: str,
    timezone: str,
):
    try:
        return create_monitor_for_domain(
            fc,
            domain_name=domain,
            urls=urls,
            name=name,
            webhook_url=webhook_url,
            email_recipients=email_recipients,
            schedule_text=schedule_text,
            timezone=timezone,
        )
    except Exception as exc:
        err_msg = str(exc)
        if "rate limit" not in err_msg.lower() and "429" not in err_msg:
            raise
        retry_match = re.search(r"retry after (\d+)s", err_msg)
        wait = int(retry_match.group(1)) + 2 if retry_match else 30
        print(f"  Rate limited on {domain}, waiting {wait}s...")
        time.sleep(wait)
        return create_monitor_for_domain(
            fc,
            domain_name=domain,
            urls=urls,
            name=name,
            webhook_url=webhook_url,
            email_recipients=email_recipients,
            schedule_text=schedule_text,
            timezone=timezone,
        )


# ---------------------------------------------------------------------------
# Monitor management
# ---------------------------------------------------------------------------

def _obj_to_dict(obj: Any) -> Dict[str, Any]:
    """Convert Pydantic model or dict to dict for display."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return {"id": getattr(obj, "id", "?"), "name": getattr(obj, "name", "?")}


def list_all_monitors(fc) -> List[Dict[str, Any]]:
    monitors: List[Dict[str, Any]] = []
    limit = 100
    offset = 0
    while True:
        result = fc.list_monitors(limit=limit, offset=offset)
        batch = result.data if hasattr(result, "data") else result
        if not isinstance(batch, list) or not batch:
            break
        monitors.extend(_obj_to_dict(item) for item in batch)
        if len(batch) < limit:
            break
        offset += limit
    return monitors


def extract_urls_from_monitor(monitor: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for target in monitor.get("targets") or []:
        target_dict = _obj_to_dict(target)
        for url in target_dict.get("urls") or []:
            normalized = normalize_monitor_url(url)
            if normalized:
                urls.append(normalized)
    return urls


def load_domain_by_monitor_id(state_store: MonitorStateStore) -> Dict[str, str]:
    """Batch-load monitor_id -> domain_name (ponytail: one query vs N get_state calls)."""
    mapping: Dict[str, str] = {}
    if state_store.use_supabase and state_store.client:
        rows = state_store.client.fetch_rows(
            PROMO_MONITOR_STATE_TABLE,
            "monitor_id,domain_name",
            limit=1000,
        )
        for row in rows:
            domain = normalize_domain(row.get("domain_name") or "")
            if domain:
                mapping[row["monitor_id"]] = domain
        return mapping

    for monitor_id, raw in state_store._fallback.items():
        domain = normalize_domain(raw.get("domain_name") or "")
        if domain:
            mapping[monitor_id] = domain
    return mapping


def infer_domain_from_monitor(
    monitor: Dict[str, Any],
    state_store: Optional[MonitorStateStore] = None,
    domain_by_monitor_id: Optional[Dict[str, str]] = None,
) -> str:
    monitor_id = monitor.get("id") or monitor.get("monitorId") or ""
    if monitor_id and domain_by_monitor_id and monitor_id in domain_by_monitor_id:
        return domain_by_monitor_id[monitor_id]
    if monitor_id and state_store:
        state = state_store.get_state(monitor_id)
        if state and state.domain_name:
            return normalize_domain(state.domain_name)

    for url in extract_urls_from_monitor(monitor):
        domain = normalize_domain(url)
        if domain:
            return domain

    name = (monitor.get("name") or "").strip()
    if name:
        match = re.search(r"([a-z0-9][a-z0-9.-]+\.[a-z]{2,})", name.lower())
        if match:
            return normalize_domain(match.group(1))
    return ""


def dedupe_duplicate_monitors(fc, *, dry_run: bool = False) -> List[Dict[str, Any]]:
    """Remove extra monitors for domains with more than one active monitor."""
    monitors = list_all_monitors(fc)
    state_store = MonitorStateStore(load_supabase_client(PROJECT_ROOT))
    domain_by_monitor_id = load_domain_by_monitor_id(state_store)
    by_domain: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for monitor in monitors:
        domain = infer_domain_from_monitor(monitor, domain_by_monitor_id=domain_by_monitor_id)
        if domain:
            by_domain[domain].append(monitor)

    actions: List[Dict[str, Any]] = []
    for domain, group in sorted(by_domain.items()):
        if len(group) <= 1:
            continue
        group_sorted = sorted(group, key=lambda m: m.get("created_at") or m.get("createdAt") or "")
        keep = group_sorted[0]
        for duplicate in group_sorted[1:]:
            action = {
                "domain": domain,
                "keep_monitor_id": keep.get("id"),
                "delete_monitor_id": duplicate.get("id"),
                "delete_name": duplicate.get("name"),
            }
            actions.append(action)
            if dry_run:
                print(f"  [dry-run] would delete duplicate {domain}: {duplicate.get('id')} ({duplicate.get('name')})")
            else:
                _retry_firecrawl(
                    f"delete_monitor:{duplicate.get('id')}",
                    lambda mid=duplicate.get("id"): fc.delete_monitor(mid),
                )
                print(f"  Deleted duplicate {domain}: {duplicate.get('id')}")

    return actions


def sync_monitor_targets(args: argparse.Namespace) -> None:
    """Retarget existing monitors to promo/pricing subpages from staging."""
    load_env()
    fc = get_firecrawl_client()
    session, base_url = get_supabase_client()
    state_store = MonitorStateStore(load_supabase_client(PROJECT_ROOT))
    domain_by_monitor_id = load_domain_by_monitor_id(state_store)
    urls_by_domain, _ = fetch_staging_urls_by_domain(session, base_url)
    promo_by_domain = fetch_promotion_urls_by_domain(load_supabase_client())

    print("Removing duplicate monitors...")
    dedupe_duplicate_monitors(fc, dry_run=bool(args.dry_run))

    monitors = list_all_monitors(fc)
    if args.domain:
        target_domain = normalize_domain(args.domain)
        monitors = [
            m
            for m in monitors
            if infer_domain_from_monitor(m, domain_by_monitor_id=domain_by_monitor_id) == target_domain
        ]
        if not monitors:
            print(f"No monitors matched domain '{target_domain}'")
            return

    if args.limit:
        monitors = monitors[: args.limit]

    max_urls = max(1, args.max_urls)
    results: List[Dict[str, Any]] = []
    updated = skipped = errors = 0

    for i, monitor in enumerate(monitors, 1):
        monitor_id = monitor.get("id") or ""
        domain = infer_domain_from_monitor(monitor, domain_by_monitor_id=domain_by_monitor_id)
        old_urls = extract_urls_from_monitor(monitor)
        new_urls = resolve_domain_monitor_urls(
            domain=domain or "unknown",
            staging_urls=urls_by_domain.get(domain, []),
            promotion_urls=promo_by_domain.get(domain, []),
            max_urls=max_urls,
        )

        entry = {
            "monitor_id": monitor_id,
            "domain": domain,
            "old_urls": old_urls,
            "new_urls": new_urls,
            "status": "skipped",
        }

        if not domain:
            entry["status"] = "skipped_no_domain"
            results.append(entry)
            skipped += 1
            print(f"  [{i}/{len(monitors)}] SKIP (no domain): {monitor.get('name')}")
            continue

        if old_urls == new_urls:
            entry["status"] = "unchanged"
            results.append(entry)
            skipped += 1
            continue

        print(f"  [{i}/{len(monitors)}] {domain}", flush=True)
        print(f"    old: {old_urls}", flush=True)
        print(f"    new: {new_urls}", flush=True)

        if args.dry_run:
            entry["status"] = "would_update"
            results.append(entry)
            continue

        try:
            _retry_firecrawl(
                f"update_monitor:{domain}",
                lambda: fc.update_monitor(monitor_id, targets=[build_scrape_target(new_urls)]),
            )
            reset_monitor_baseline(state_store, monitor_id, domain)
            save_monitor_mapping(monitor_id, domain)
            entry["status"] = "updated"
            updated += 1
            if args.delay_secs > 0:
                time.sleep(args.delay_secs)
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)[:200]
            errors += 1

        results.append(entry)

    summary = {
        "dry_run": bool(args.dry_run),
        "monitors_seen": len(monitors),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "results": results,
    }
    _write_report("sync_targets", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False, indent=2))


def _write_report(kind: str, payload: Dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = OUTPUT_DIR / f"{REPORT_PREFIX}_{kind}_{timestamp}.json"
    report_path.write_text(json.dumps({"status": "completed", **payload}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. Report: {report_path}")


def list_monitors(args: argparse.Namespace) -> None:
    """List all monitors."""
    load_env()
    fc = get_firecrawl_client()
    monitors = list_all_monitors(fc)

    if not monitors:
        print("No monitors found.")
        return

    for m in monitors:
        mid = m.get("id", "?")
        name = m.get("name", "?")
        status = m.get("status", "?")
        next_run = m.get("next_run_at") or m.get("nextRunAt") or "?"
        urls = extract_urls_from_monitor(m)
        url_hint = urls[0] if urls else "?"
        if len(urls) > 1:
            url_hint = f"{url_hint} (+{len(urls) - 1})"
        print(f"  {mid}  [{status}]  {name}  urls: {url_hint}  next: {next_run}")


def get_monitor_checks(args: argparse.Namespace) -> None:
    """List checks for a monitor."""
    load_env()
    fc = get_firecrawl_client()

    checks = fc.list_monitor_checks(args.monitor_id)

    if isinstance(checks, dict):
        data = checks.get("data", checks)
    else:
        data = getattr(checks, "data", checks)

    if isinstance(data, list):
        for c in data:
            d = _obj_to_dict(c)
            cid = d.get("id", "?")
            status = d.get("status", "?")
            summary = d.get("summary", {})
            print(f"  {cid}  [{status}]  {summary}")
    else:
        print(json.dumps(_obj_to_dict(data), indent=2, default=str))


def get_check_detail(args: argparse.Namespace) -> None:
    """Get detailed check results with page diffs."""
    load_env()
    fc = get_firecrawl_client()

    check = fc.get_monitor_check(
        args.monitor_id,
        args.check_id,
        limit=args.limit or 25,
        status=args.status,
    )

    data = check.data if hasattr(check, "data") else check
    d = _obj_to_dict(data)

    if isinstance(d, dict):
        pages = d.get("pages", [])
        summary = d.get("summary", {})
        print(f"Check: {d.get('id', '?')}")
        print(f"Status: {d.get('status', '?')}")
        print(f"Summary: {json.dumps(summary, indent=2)}")
        print(f"Pages ({len(pages)}):")
        for p in pages:
            pd = _obj_to_dict(p)
            url = pd.get("url", "?")
            status = pd.get("status", "?")
            judgment = pd.get("judgment")
            diff = pd.get("diff")

            print(f"\n  [{status}] {url}")
            if judgment:
                j = _obj_to_dict(judgment) if not isinstance(judgment, dict) else judgment
                meaningful = j.get("meaningful", False)
                reason = j.get("reason", "")
                print(f"    Meaningful: {meaningful} - {reason}")
            if diff:
                dd = _obj_to_dict(diff) if not isinstance(diff, dict) else diff
                text_diff = dd.get("text", "")
                json_diff = dd.get("json", {})
                if text_diff:
                    print(f"    Diff (markdown):\n{text_diff[:500]}")
                if json_diff:
                    print(f"    Diff (JSON): {json.dumps(json_diff, indent=4)[:500]}")
    else:
        print(json.dumps(d, indent=2, default=str))


def delete_monitor(args: argparse.Namespace) -> None:
    """Delete a monitor."""
    load_env()
    fc = get_firecrawl_client()
    fc.delete_monitor(args.monitor_id)
    print(f"Deleted monitor: {args.monitor_id}")


def run_monitor(args: argparse.Namespace) -> None:
    """Trigger a manual run of a monitor."""
    load_env()
    fc = get_firecrawl_client()
    result = fc.run_monitor(args.monitor_id)
    print(f"Triggered run for monitor: {args.monitor_id}")
    data = result.data if hasattr(result, "data") else result
    print(json.dumps(_obj_to_dict(data), indent=2, default=str))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Firecrawl Monitor for promo website change tracking"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create_all = sub.add_parser("create-all", help="Create monitors for all domains in promo_website_staging")
    p_create_all.add_argument("--domain", help="Only create for this domain")
    p_create_all.add_argument("--limit", type=int, help="Limit number of domains")
    p_create_all.add_argument("--max-urls", type=int, default=2, help="Promo URLs per monitor (default: 2)")
    p_create_all.add_argument("--schedule", default="daily", help="Schedule (e.g. 'daily', 'every 6 hours', cron expression)")
    p_create_all.add_argument("--timezone", default="America/Phoenix", help="Timezone for schedule")
    p_create_all.add_argument("--email", help="Comma-separated email recipients for notifications")
    p_create_all.add_argument("--webhook", help="Webhook URL for notifications")

    p_create = sub.add_parser("create", help="Create monitor for a single domain")
    p_create.add_argument("--domain", required=True, help="Domain name (e.g. example-medspa.com)")
    p_create.add_argument("--url", help="Override monitor URL(s), comma-separated")
    p_create.add_argument("--max-urls", type=int, default=2, help="Promo URLs when picking from staging (default: 2)")
    p_create.add_argument("--name", help="Monitor display name")
    p_create.add_argument("--schedule", default="daily", help="Schedule")
    p_create.add_argument("--timezone", default="America/Phoenix", help="Timezone")
    p_create.add_argument("--email", help="Comma-separated email recipients")
    p_create.add_argument("--webhook", help="Webhook URL")

    p_sync = sub.add_parser("sync-targets", help="Retarget monitors to promo/pricing subpages from staging")
    p_sync.add_argument("--dry-run", action="store_true", help="Preview URL changes without updating Firecrawl")
    p_sync.add_argument("--domain", help="Only sync monitors for this domain")
    p_sync.add_argument("--limit", type=int, help="Process only the first N monitors")
    p_sync.add_argument("--max-urls", type=int, default=2, help="Promo URLs per monitor (default: 2)")
    p_sync.add_argument(
        "--delay-secs",
        type=float,
        default=25.0,
        help="Seconds to wait between Firecrawl update calls (default: 25, ~3 req/min)",
    )

    sub.add_parser("list", help="List all monitors")

    p_checks = sub.add_parser("checks", help="List checks for a monitor")
    p_checks.add_argument("--monitor-id", required=True)

    p_detail = sub.add_parser("check-detail", help="Get check detail with page diffs")
    p_detail.add_argument("--monitor-id", required=True)
    p_detail.add_argument("--check-id", required=True)
    p_detail.add_argument("--limit", type=int, default=25)
    p_detail.add_argument("--status", help="Filter pages by status (same/changed/new/removed/error)")

    p_delete = sub.add_parser("delete", help="Delete a monitor")
    p_delete.add_argument("--monitor-id", required=True)

    p_run = sub.add_parser("run", help="Trigger manual monitor run")
    p_run.add_argument("--monitor-id", required=True)

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.command == "create-all":
        create_monitors_for_all_domains(args)
    elif args.command == "create":
        load_env()
        fc = get_firecrawl_client()
        domain = normalize_domain(args.domain)
        name = args.name or ""
        if args.url:
            urls = [u.strip() for u in args.url.split(",") if u.strip()]
        else:
            session, base_url = get_supabase_client()
            urls_by_domain, names_by_domain = fetch_staging_urls_by_domain(session, base_url)
            promo_by_domain = fetch_promotion_urls_by_domain(load_supabase_client())
            urls = resolve_domain_monitor_urls(
                domain=domain,
                staging_urls=urls_by_domain.get(domain, []),
                promotion_urls=promo_by_domain.get(domain, []),
                max_urls=max(1, args.max_urls),
            )
            if not name:
                name = names_by_domain.get(domain, "")
        email_recipients = [e.strip() for e in args.email.split(",")] if args.email else None
        monitor = create_monitor_for_domain(
            fc,
            domain_name=domain,
            urls=urls,
            name=args.name or name,
            webhook_url=args.webhook,
            email_recipients=email_recipients,
            schedule_text=args.schedule,
            timezone=args.timezone,
        )
        d = _obj_to_dict(monitor.data if hasattr(monitor, "data") else monitor)
        mid = d.get("id", "?")
        print(f"Created monitor: {mid}  urls={urls}")
        save_monitor_mapping(mid, domain)
        print(json.dumps(d, indent=2, default=str))
    elif args.command == "sync-targets":
        sync_monitor_targets(args)
    elif args.command == "list":
        list_monitors(args)
    elif args.command == "checks":
        get_monitor_checks(args)
    elif args.command == "check-detail":
        get_check_detail(args)
    elif args.command == "delete":
        delete_monitor(args)
    elif args.command == "run":
        run_monitor(args)
    else:
        print(f"Unknown command: {args.command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
