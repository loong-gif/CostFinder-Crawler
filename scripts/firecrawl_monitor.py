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
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from crawler.staging_recrawl import MonitorStateStore, load_supabase_client

OUTPUT_DIR = PROJECT_ROOT / "output" / "monitor_results"
REPORT_PREFIX = "firecrawl_monitor"


def load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def get_firecrawl_client():
    """Initialize Firecrawl client with API key from env."""
    from firecrawl import Firecrawl

    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing FIRECRAWL_API_KEY. Add it to .env or export it.\n"
            "Get your key at: https://firecrawl.dev/app/api-keys"
        )
    return Firecrawl(api_key=api_key)


def get_supabase_client():
    """Initialize Supabase REST client."""
    import requests

    base_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
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
                    "price": {"type": "string"},
                    "description": {"type": "string"},
                    "valid_through": {"type": "string"},
                },
            },
        },
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "price": {"type": "string"},
                },
            },
        },
        "memberships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "price": {"type": "string"},
                    "benefits": {"type": "string"},
                },
            },
        },
    },
}

EXTRACTION_PROMPT = (
    "Extract all pricing, offers/specials/promotions, service prices, "
    "and membership plans. Include the item name, price, description, "
    "and any expiration date. Return empty arrays for categories with no data."
)


# ---------------------------------------------------------------------------
# Monitor creation
# ---------------------------------------------------------------------------

def create_monitor_for_domain(
    client,
    domain_name: str,
    website_url: str,
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

    # Build target with mixed-mode change tracking (JSON + markdown diff)
    target = {
        "type": "scrape",
        "urls": [website_url],
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
        },
    }

    # Build notification config
    notification: Dict[str, Any] = {}
    if email_recipients:
        notification["email"] = {
            "enabled": True,
            "recipients": email_recipients,
            "includeDiffs": True,
        }

    webhook: Optional[Dict[str, Any]] = None
    if webhook_url:
        webhook = {
            "url": webhook_url,
            "events": ["monitor.page", "monitor.check.completed"],
        }

    monitor = client.create_monitor(
        name=monitor_name,
        schedule={"text": schedule_text, "timezone": timezone},
        goal=goal,
        targets=[target],
        notification=notification if notification else None,
        webhook=webhook,
    )

    return monitor


def create_monitors_for_all_domains(args: argparse.Namespace) -> None:
    """Create monitors for all domains in promo_website_staging."""
    load_env()
    fc = get_firecrawl_client()
    session, base_url = get_supabase_client()

    # Fetch unique domains from promo_website_staging
    print("Fetching domains from promo_website_staging...")
    resp = session.get(
        f"{base_url}/promo_website_staging",
        params={
            "select": "domain_name,name",
            "domain_name": "not.is.null",
            "order": "domain_name.asc",
        },
        timeout=60,
    )
    resp.raise_for_status()
    rows = resp.json()

    # Dedupe by domain
    domains: Dict[str, str] = {}
    for row in rows:
        d = (row.get("domain_name") or "").strip().lower()
        n = (row.get("name") or "").strip()
        if d and d not in domains:
            domains[d] = n

    if args.limit:
        domains = dict(list(domains.items())[: args.limit])

    if args.domain:
        d = args.domain.strip().lower()
        if d in domains:
            domains = {d: domains[d]}
        else:
            print(f"Domain '{d}' not found in promo_website_staging")
            return

    # Fetch existing monitors to avoid duplicates
    print("Checking existing monitors...")
    existing_result = fc.list_monitors()
    existing_monitors = existing_result.data if hasattr(existing_result, "data") else existing_result
    existing_names = set()
    if isinstance(existing_monitors, list):
        for m in existing_monitors:
            d = _obj_to_dict(m)
            name = d.get("name", "")
            existing_names.add(name)

    # Filter out domains that already have monitors
    new_domains = {}
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

    email_recipients = None
    if args.email:
        email_recipients = [e.strip() for e in args.email.split(",")]

    webhook_url = args.webhook

    results: List[Dict[str, Any]] = []
    errors = 0

    for i, (domain, name) in enumerate(domains.items(), 1):
        website_url = f"https://{domain}"
        try:
            monitor = create_monitor_for_domain(
                fc,
                domain_name=domain,
                website_url=website_url,
                name=name,
                webhook_url=webhook_url,
                email_recipients=email_recipients,
                schedule_text=args.schedule,
                timezone=args.timezone,
            )
            monitor_data = monitor.data if hasattr(monitor, "data") else monitor
            md = _obj_to_dict(monitor_data)
            mid = md.get("id", "unknown")
            print(f"  [{i}/{len(domains)}] Created: {domain} -> {mid}")
            save_monitor_mapping(mid, domain)
            results.append({
                "domain": domain,
                "monitor_id": mid,
                "status": "created",
            })
        except Exception as e:
            err_msg = str(e)
            if "Rate Limit" in err_msg or "rate limit" in err_msg.lower():
                # Extract retry-after from error if available
                import re
                retry_match = re.search(r"retry after (\d+)s", err_msg)
                wait = int(retry_match.group(1)) + 2 if retry_match else 30
                print(f"  [{i}/{len(domains)}] Rate limited, waiting {wait}s...")
                time.sleep(wait)
                # Retry once
                try:
                    monitor = create_monitor_for_domain(
                        fc,
                        domain_name=domain,
                        website_url=f"https://{domain}",
                        name=name,
                        webhook_url=webhook_url,
                        email_recipients=email_recipients,
                        schedule_text=args.schedule,
                        timezone=args.timezone,
                    )
                    monitor_data = monitor.data if hasattr(monitor, "data") else monitor
                    md = _obj_to_dict(monitor_data)
                    mid = md.get("id", "unknown")
                    print(f"  [{i}/{len(domains)}] Created (retry): {domain} -> {mid}")
                    save_monitor_mapping(mid, domain)
                    results.append({"domain": domain, "monitor_id": mid, "status": "created"})
                except Exception as e2:
                    print(f"  [{i}/{len(domains)}] ERROR (retry failed): {domain} -> {e2}")
                    results.append({"domain": domain, "monitor_id": None, "status": "error", "error": str(e2)[:200]})
                    errors += 1
            else:
                print(f"  [{i}/{len(domains)}] ERROR: {domain} -> {e}")
                results.append({"domain": domain, "monitor_id": None, "status": "error", "error": str(e)[:200]})
                errors += 1

    # Save report
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = OUTPUT_DIR / f"{REPORT_PREFIX}_create_{timestamp}.json"
    report = {
        "status": "completed",
        "total": len(domains),
        "created": len([r for r in results if r["status"] == "created"]),
        "errors": errors,
        "results": results,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nDone. Report: {report_path}")


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


def list_monitors(args: argparse.Namespace) -> None:
    """List all monitors."""
    load_env()
    fc = get_firecrawl_client()

    result = fc.list_monitors()
    monitors = result.data if hasattr(result, "data") else result

    if not monitors:
        print("No monitors found.")
        return

    if isinstance(monitors, list):
        for m in monitors:
            d = _obj_to_dict(m)
            mid = d.get("id", "?")
            name = d.get("name", "?")
            status = d.get("status", "?")
            next_run = d.get("next_run_at") or d.get("nextRunAt") or "?"
            print(f"  {mid}  [{status}]  {name}  next: {next_run}")
    else:
        print(json.dumps(_obj_to_dict(monitors), indent=2, default=str))


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

    # create-all
    p_create_all = sub.add_parser("create-all", help="Create monitors for all domains in promo_website_staging")
    p_create_all.add_argument("--domain", help="Only create for this domain")
    p_create_all.add_argument("--limit", type=int, help="Limit number of domains")
    p_create_all.add_argument("--schedule", default="daily", help="Schedule (e.g. 'daily', 'every 6 hours', cron expression)")
    p_create_all.add_argument("--timezone", default="America/Phoenix", help="Timezone for schedule")
    p_create_all.add_argument("--email", help="Comma-separated email recipients for notifications")
    p_create_all.add_argument("--webhook", help="Webhook URL for notifications")

    # create (single)
    p_create = sub.add_parser("create", help="Create monitor for a single domain")
    p_create.add_argument("--domain", required=True, help="Domain name (e.g. example-medspa.com)")
    p_create.add_argument("--url", help="Full website URL (default: https://<domain>)")
    p_create.add_argument("--name", help="Monitor display name")
    p_create.add_argument("--schedule", default="daily", help="Schedule")
    p_create.add_argument("--timezone", default="America/Phoenix", help="Timezone")
    p_create.add_argument("--email", help="Comma-separated email recipients")
    p_create.add_argument("--webhook", help="Webhook URL")

    # list
    sub.add_parser("list", help="List all monitors")

    # checks
    p_checks = sub.add_parser("checks", help="List checks for a monitor")
    p_checks.add_argument("--monitor-id", required=True)

    # check-detail
    p_detail = sub.add_parser("check-detail", help="Get check detail with page diffs")
    p_detail.add_argument("--monitor-id", required=True)
    p_detail.add_argument("--check-id", required=True)
    p_detail.add_argument("--limit", type=int, default=25)
    p_detail.add_argument("--status", help="Filter pages by status (same/changed/new/removed/error)")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a monitor")
    p_delete.add_argument("--monitor-id", required=True)

    # run
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
        domain = args.domain.strip().lower()
        url = args.url or f"https://{domain}"
        email_recipients = [e.strip() for e in args.email.split(",")] if args.email else None
        monitor = create_monitor_for_domain(
            fc,
            domain_name=domain,
            website_url=url,
            name=args.name or "",
            webhook_url=args.webhook,
            email_recipients=email_recipients,
            schedule_text=args.schedule,
            timezone=args.timezone,
        )
        data = monitor.data if hasattr(monitor, "data") else monitor
        d = _obj_to_dict(data)
        mid = d.get("id", "?")
        print(f"Created monitor: {mid}")
        save_monitor_mapping(mid, domain)
        print(json.dumps(d, indent=2, default=str))
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
