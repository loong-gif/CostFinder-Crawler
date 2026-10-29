#!/usr/bin/env python3
"""Promotion monitoring, file evidence/plans, and pending SQL files (no database writes)."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from requests import HTTPError
from scripts.firecrawl_self_hosted_monitor import load_config, run_monitor
from utils.monitor_price_extract import extract_evidence, render_page
from utils.monitor_price_plan import build_plan
from utils.monitor_price_store import atomic_json, digest, manual_protections, now, read_json, run_lock
from utils.monitor_price_writer import SERVICE_COLUMNS, _protect, normalize_source_url, source_graph
from utils.monitor_pending_sql import render_pending, settle
from utils.monitor_image_gate import image_write_block, capture_is_current
from utils.monitor_rendered_browser import confirmed_not_found
from utils.monitor_price_retry import RetryLimits
from utils.monitor_price_visual_receipts import reviewed_browser
from utils.pending_sql import pending_dir
from utils.supabase_rest import SupabaseRestClient, get_supabase_secret_key

PROJECT_ID = "kdlpkjzcnbkjcvwsvlwn"


def database_snapshot() -> dict:
    base = os.getenv("SUPABASE_URL", "")
    if urlsplit(base).hostname != PROJECT_ID + ".supabase.co":
        raise RuntimeError("database_project_mismatch")
    client = SupabaseRestClient(base, get_supabase_secret_key())
    def all_rows(table, columns="*"):
        values, offset = [], 0
        pk = {"clinic_promotions":"promotion_id", "promo_offer_items":"offer_item_id",
              "master_business_info":"business_id", "clinic_services":"service_id",
              "promo_offer_terms":"offer_id", "clinic_memberships":"plan_id"}.get(table, "id")
        while True:
            rows = client.fetch_rows(table, columns, limit=500, offset=offset, order=f"{pk}.asc")
            values.extend(rows)
            if len(rows) < 500:
                return values
            offset += len(rows)
    snapshot = {name:all_rows(table) for name,table in {
        "promotions":"clinic_promotions", "offers":"promo_offer_master", "items":"promo_offer_items",
        "businesses":"master_business_info"}.items()}
    snapshot["services"] = all_rows("clinic_services", SERVICE_COLUMNS)
    snapshot["terms"] = all_rows("promo_offer_terms")
    # Read only source ownership, never membership fees or benefits.
    snapshot["memberships"] = all_rows("clinic_memberships", "plan_id,business_id,source_url")
    names = client.rpc_read("fn_clinic_service_names")
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        raise RuntimeError("service_names_unavailable")
    snapshot["service_names"] = names
    return snapshot


def check_promotion_source(url: str, snapshot: dict) -> None:
    canonical = normalize_source_url(url)
    if re.search(r"(?:^|[-_/])memberships?(?:[-_/]|$)", urlsplit(url).path, re.I):
        raise RuntimeError("membership_source_requires_review")
    for row in snapshot.get("memberships", []):
        source = row.get("source_url")
        if source:
            try:
                matches = normalize_source_url(source) == canonical
            except ValueError:
                continue
            if matches:
                raise RuntimeError("membership_source_requires_review")


def bind_source(url: str, snapshot: dict) -> dict:
    check_promotion_source(url, snapshot)
    canonical = normalize_source_url(url)
    matches = set()
    for key in ("promotions", "services"):
        for row in snapshot[key]:
            source = row.get("source_url")
            if source:
                try:
                    if normalize_source_url(source) == canonical:
                        matches.add(row["business_id"])
                except ValueError:
                    pass
    if not matches:
        domain = urlsplit(url).hostname.lower().removeprefix("www.")
        for row in snapshot["businesses"]:
            website = str(row.get("website") or "")
            host = urlsplit(website if "://" in website else "https://"+website).hostname
            if host and host.lower().removeprefix("www.") == domain:
                matches.add(row["business_id"])
    if len(matches) != 1:
        raise RuntimeError("source_business_ambiguous" if matches else "source_business_missing")
    return {"business_id":matches.pop(), "url":url}


def snapshot_business_graph(snapshot: dict, business_id: int) -> dict:
    promotions = [r for r in snapshot["promotions"] if r["business_id"] == business_id]
    promotion_ids = {r["promotion_id"] for r in promotions}
    offers = [r for r in snapshot["offers"] if r["business_id"] == business_id or r.get("promotion_id") in promotion_ids]
    offer_ids = {r["id"] for r in offers}
    return {"promotions":promotions, "offers":offers,
            "items":[r for r in snapshot["items"] if r["offer_id"] in offer_ids],
            "services":[r for r in snapshot["services"] if r["business_id"] == business_id],
            "terms":[r for r in snapshot.get("terms", []) if r.get("offer_id") in offer_ids]}


def verified_batch_results(root: Path, snapshot: dict) -> list[dict]:
    """Acknowledge applied fields against today's DB without replaying old DML.

    Receipts cover specific records. Unresolved offers on the same page still
    require review; a prior partial batch does not prove page completeness.
    """
    results = []
    for receipt in read_json(root / "verified_batch_receipts.json", []):
        matches, differences = [], []
        for expected in receipt["records"]:
            actual = next((row for row in snapshot.get(expected["group"], [])
                           if row[expected["pk"]] == expected["id"]), None)
            changed = [field for field, value in expected["values"].items()
                       if actual is None or actual.get(field) != value]
            ref = {"group":expected["group"], "id":expected["id"]}
            if changed:
                differences.append({**ref, "fields":changed})
            else:
                matches.append(ref)
        results.append({"batch_id":receipt["batch_id"],"url":receipt["url"],
                        "verified_at":receipt["verified_at"],"database_read_at":now(),
                        "status":"already_synchronized" if not differences else "changed_since_verification",
                        "confirmed_records":matches,"changed_records":differences,
                        "evidence_paths":receipt.get("evidence_paths",[])})
    return results


def retry_failed_scrape_urls(root: Path, config: dict) -> set[str]:
    """Select failed pages below the per-source hourly retry limit."""
    configured = {url for monitor in config["monitors"].values()
                  for group in monitor.get("targets", []) for url in group.get("urls", [])}
    limits = RetryLimits(root)
    limits.refresh_scrapes(configured)
    needed = {value["url"] for value in limits.state["scrape"].values()
              if not limits.paused("scrape", value["url"])}
    for path in (root / "events").glob("*.json"):
        event = read_json(path)
        if (event.get("status") == "retryable" and event.get("retry_reason") == "fresh_scrape_required"
                and event.get("url") in configured
                and not limits.paused("scrape", event["url"])
                and not limits.paused("processing", event["url"])):
            needed.add(event["url"])
    return needed


def expired_active_promotion_urls(snapshot: dict) -> set[str]:
    """Source URLs for promotions that are still active but past campaign_end_date."""
    today = date.today()
    urls: set[str] = set()
    for promotion in snapshot.get("promotions", []):
        if promotion.get("is_active") is not True or not promotion.get("source_url"):
            continue
        end_date = str(promotion.get("campaign_end_date") or "")[:10]
        try:
            if end_date and date.fromisoformat(end_date) < today:
                urls.add(promotion["source_url"])
        except ValueError:
            continue
    return urls


def enqueue_expired_campaign_checks(root: Path, snapshot: dict, config: dict) -> int:
    """Queue one fresh evidence check for each active, dated campaign that expired."""
    today = datetime.now().astimezone().date()
    newest: dict[str, tuple[datetime, Path, str]] = {}
    for path in (root / "evidence").glob("*.json"):
        try:
            artifact = read_json(path)
            captured = datetime.fromisoformat(str(artifact["captured_at"]).replace("Z", "+00:00"))
            if captured.astimezone().date() != today:
                continue
            url = artifact["url"]
            key = normalize_source_url(url)
            if key not in newest or captured > newest[key][0]:
                newest[key] = (captured, path, url)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    events_dir = root / "events"
    existing_ids = {path.stem for path in events_dir.glob("*.json")}
    queued = 0
    for promotion in snapshot.get("promotions", []):
        if promotion.get("is_active") is not True:
            continue
        end_date = str(promotion.get("campaign_end_date") or "")[:10]
        try:
            if not end_date or date.fromisoformat(end_date) >= today:
                continue
        except ValueError:
            continue
        url = promotion.get("source_url")
        try:
            key = normalize_source_url(url)
        except ValueError:
            continue
        if key not in newest:
            continue
        event_id = digest(["campaign-expiry", promotion["promotion_id"], end_date, newest[key][1].stem])
        if event_id in existing_ids:
            continue
        artifact_path = newest[key][1]
        event = {"event_id":event_id,"url":url,"promotion_id":promotion["promotion_id"],
                 "artifact_id":artifact_path.stem,"artifact_path":str(artifact_path),
                 "status":"pending","change_type":"campaign_expiry_check",
                 "created_at":now(),"expired_campaign_end_date":end_date}
        atomic_json(events_dir / (event_id+".json"),event)
        queued += 1
    return queued



def data_quality_recheck_urls(root: Path) -> set[str]:
    queue = read_json(root / "data_quality" / "recheck_queue.json", {})
    entries = queue.get("entries", []) if isinstance(queue, dict) else queue
    return {row["source_url"] for row in entries if row.get("source_kind") == "promotion"
            and row.get("status") in {"pending", "review"} and row.get("source_url")}


def enqueue_data_quality_rechecks(root: Path) -> int:
    """The shared queue owns status; Monitor adds durable evidence events only."""
    queue = read_json(root / "data_quality" / "recheck_queue.json", {})
    entries = queue.get("entries", []) if isinstance(queue, dict) else queue
    newest = {}
    for path in (root / "evidence").glob("*.json"):
        artifact = read_json(path)
        try:
            captured = datetime.fromisoformat(artifact["captured_at"].replace("Z", "+00:00"))
            if captured.astimezone().date() != datetime.now().astimezone().date():
                continue
            canonical = normalize_source_url(artifact["url"])
            if canonical not in newest or captured > newest[canonical][0]:
                newest[canonical] = (captured, path)
        except (KeyError, TypeError, ValueError):
            continue
    queued = 0
    for row in entries:
        if row.get("source_kind") != "promotion" or row.get("status") not in {"pending", "review"}:
            continue
        key = normalize_source_url(row["source_url"])
        if key not in newest:
            continue
        artifact_path = newest[key][1]
        event_id = digest(["data-quality-recheck", key, row.get("requested_at"), artifact_path.stem])
        event_path = root / "events" / (event_id + ".json")
        if event_path.exists():
            continue
        atomic_json(event_path, {"event_id":event_id,"url":row["source_url"],"status":"pending",
                    "change_type":"data_quality_recheck","created_at":now(),
                    "artifact_id":artifact_path.stem,"artifact_path":str(artifact_path),
                    "quality_reasons":row.get("reasons", []),"quality_requested_at":row.get("requested_at")})
        queued += 1
    return queued

def prepare_event(path: Path, snapshot: dict, protections: dict, root: Path) -> dict:
    event = read_json(path)
    directory = root / "sync" / event["event_id"]
    plan_path = directory / "plan.json"
    archived = False
    try:
        # Scope checks precede the cache: old ready plans must not bypass them.
        source = bind_source(event["url"], snapshot)
        if event.get("retry_reason") == "fresh_scrape_required":
            threshold = datetime.fromisoformat(event["fresh_evidence_after"].replace("Z", "+00:00"))
            newest = None
            for artifact_path in (root / "evidence").glob("*.json"):
                artifact = read_json(artifact_path)
                if normalize_source_url(artifact["url"]) != normalize_source_url(event["url"]):
                    continue
                captured = datetime.fromisoformat(artifact["captured_at"].replace("Z", "+00:00"))
                if captured > threshold and datetime.now(timezone.utc) - captured <= timedelta(hours=24):
                    if newest is None or captured > newest[0]:
                        newest = (captured, artifact_path)
            if newest is None:
                raise RuntimeError("fresh_scrape_required")
            event["artifact_path"] = str(newest[1])
            event["artifact_id"] = newest[1].stem
        if plan_path.exists():
            cached = read_json(plan_path)
            if event.get("receipt", {}).get("resume_required"):
                return cached
            prepared = datetime.fromisoformat(cached.get("prepared_at", "1970-01-01T00:00:00+00:00"))
            stale = cached.get("status") == "ready" and datetime.now(timezone.utc) - prepared > timedelta(hours=24)
            cached_browser = read_json(directory / "browser.json", {})
            if cached_browser.get("final_url"):
                check_promotion_source(cached_browser["final_url"], snapshot)
            stale = stale or not capture_is_current(cached_browser)
            legacy_image = image_write_block(cached,pending_dir(root)) in {"legacy_image_evidence_requires_refresh", "breakpoint_coverage_incomplete", "variant_capture_proof_mismatch"}
            policy_changed = (cached.get("image_policy") is not None and
                cached["image_policy"].get("image_write_enabled") !=
                (read_json(root / "price_sync_control.json",{}).get("image_write_enabled") is True))
            if not stale and not legacy_image and not policy_changed and event.get("status") != "retryable":
                cached_browser = read_json(directory / "browser.json", {})
                if cached_browser.get("final_url"):
                    check_promotion_source(cached_browser["final_url"], snapshot)
                return cached
            attempt = int(event.get("attempt", 0)) + 1
            atomic_json(directory / f"plan_attempt_{attempt}.json", cached)
            event["attempt"] = attempt
            archived = True
        artifact = read_json(Path(event["artifact_path"]))
        graph = source_graph(snapshot_business_graph(snapshot, source["business_id"]), source)
        rendered_path = artifact.get("rendered",{}).get("browser_path")
        browser = read_json(Path(rendered_path),{}) if rendered_path else None
        if not browser:
            verified = reviewed_browser(root, artifact, directory)
            if verified:
                receipt = next(r for r in read_json(root / "visual_verifications.json", [])
                               if r.get("url") == artifact.get("url") and r.get("artifact_captured_at") == artifact.get("captured_at")
                               and r.get("artifact_markdown") == artifact.get("markdown"))
                if all(Path(f).is_file() for f in receipt.get("screenshots_viewed", [])):
                    browser = {**verified, "captured_at":artifact["captured_at"], "verified_capture":True,
                               "url":artifact["url"], "visual_receipt_path":str(root / "visual_verifications.json"),
                               "verified_artifact_path":event["artifact_path"], "breakpoint_coverage":receipt.get("breakpoint_coverage"),
                               "complete":bool(receipt.get("breakpoint_coverage") and
                                      all(v.get("complete") for v in receipt["breakpoint_coverage"].values()))}
        if (not browser or (not capture_is_current(browser) and not browser.get("verified_capture")) or not browser.get("accessible") or
                event.get("retry_reason") in {"image_capture_incomplete","image_stage_timeout","image_tiles_missing"}):
            browser = render_page(event["url"], directory, business_id=source.get("business_id"))
        ocr_forced = any(p.get("is_ocr_required") is True for p in graph["promotions"])
        if ocr_forced and not browser.get("image_review_required"):
            browser = render_page(event["url"], directory, force_ocr=True, business_id=source.get("business_id"))
        browser["ocr_forced"] = ocr_forced
        browser["dual_cache_path"] = str(root / "visual_cache" / (digest(normalize_source_url(event["url"]))+".json"))
        browser["image_write_enabled"] = read_json(root / "price_sync_control.json",{}).get("image_write_enabled") is True
        atomic_json(directory / "browser.json",browser)
        not_found = confirmed_not_found(browser)
        if not browser.get("accessible") and not not_found:
            raise RuntimeError("page_not_accessible")
        # Redirects across a clinic boundary cannot silently rebind an offer.
        if urlsplit(browser["final_url"]).hostname.lower().removeprefix("www.") != urlsplit(event["url"]).hostname.lower().removeprefix("www."):
            raise RuntimeError("source_redirect_requires_review")
        check_promotion_source(browser["final_url"], snapshot)
        if not_found:
            active = [p for p in graph["promotions"] if p.get("is_active") is True]
            if len(active) != 1:
                raise RuntimeError("not_found_source_match_requires_review")
            parent_id = active[0]["promotion_id"]
            operations = [{"table":"promo_offer_master","action":"update","id":o["id"],
                           "values":{"is_active":False}} for o in graph["offers"]
                          if o.get("is_active") is True and o["promotion_id"] == parent_id]
            operations.append({"table":"clinic_promotions","action":"update","id":parent_id,
                               "values":{"is_active":False,"is_offer_extracted":False}})
            _protect(graph,operations,protections)
            plan = {"schema_version":1,"status":"ready","source":source,"before":graph,
                    "operations":operations,"review":[],"evidence_summary":{"confirmed_http_and_visible_404":True},
                    "image_policy":{"version":1,"depends_on_images":False,"image_write_enabled":False}}
            atomic_json(directory / "evidence.json",{"url":event["url"],"http_status":404,
                        "visible_not_found":browser["text"],"screenshot_path":browser.get("screenshot_path")})
        else:
            evidence = extract_evidence(artifact, browser, directory, None, snapshot["service_names"])
            plan = build_plan(source, graph, evidence, protections)
        plan["event_id"] = event["event_id"]
        plan["evidence_path"] = str(directory / "evidence.json")
        plan["prepared_at"] = now()
        if plan.get("image_retry_reason") and plan["status"] != "ready":
            plan["review"].append({"reason":plan["image_retry_reason"],"blocking":False})
    except Exception as exc:
        # Exception text can contain credential-bearing request URLs; export
        # only our own categories or the exception class, never raw HTTP text.
        category = str(exc) if isinstance(exc, RuntimeError) and str(exc).replace("_", "").isalnum() else type(exc).__name__
        if isinstance(exc,RuntimeError) and (str(exc).startswith("Gemini HTTP 429") or str(exc).startswith("LLM HTTP 429")):
            category = "extraction_rate_limited"
        if isinstance(exc, HTTPError) and exc.response is not None:
            category = {402:"extraction_quota_exhausted",429:"extraction_rate_limited",
                        401:"extraction_credentials_rejected",403:"extraction_credentials_rejected"}.get(exc.response.status_code,category)
        plan = {"schema_version":1,"status":"review", "source":{"url":event["url"]},
                "operations":[],"review":[{"reason":category}],"event_id":event["event_id"],"prepared_at":now()}
    if plan_path.exists() and not archived:
        attempt = int(event.get("attempt", 0)) + 1
        atomic_json(directory / f"plan_attempt_{attempt}.json", read_json(plan_path))
        event["attempt"] = attempt
    atomic_json(plan_path, plan)
    transient = {"rendered_page_failed","page_not_accessible","rendered_page_timeout","rendered_browser_unavailable",
                 "TimeoutExpired","TimeoutError","HTTPError","ConnectionError",
                 "extraction_credentials_missing","extraction_rate_limited","extraction_quota_exhausted","JSONDecodeError",
                 "fresh_scrape_required","image_stage_timeout","image_tiles_missing","image_capture_incomplete"}
    reason = (plan.get("review") or [{}])[-1].get("reason")
    status = "planned" if plan["status"] == "ready" else "retryable" if reason in transient else "review"
    event.update(status=status, plan_path=str(plan_path))
    atomic_json(path, event)
    return plan


def process_events(root: Path, snapshot: dict, *, write_enabled: bool, event_limit: int | None = None,
                   retryable_only: bool = False, retry_probe_urls: set[str] | None = None) -> dict:
    limits = RetryLimits(root)
    probes = {normalize_source_url(url) for url in (retry_probe_urls or set())}
    protection_path = root / "manual_protections.json"
    protections = manual_protections(read_json(protection_path, {}))
    atomic_json(protection_path, protections)
    pending = []
    for path in (root / "events").glob("*.json"):
        event = read_json(path)
        if event.get("status") == "pending_sql":
            # SQL 文件已由会话中的 agent 执行并移入 applied/ 则事件完成；文件丢失则重新渲染
            settled = settle(event["receipt"], root)
            if settled != "pending_sql":
                event["status"] = settled
                if settled == "retryable":
                    event.update(retry_reason="pending_sql_missing")
                    event.pop("receipt")
                atomic_json(path, event)
        if event.get("status") not in {"completed", "superseded", "pending_sql"}:
            pending.append((event.get("created_at", ""), path, event))
    pending.sort(key=lambda row:row[0])
    latest = {}
    for _, path, event in pending:
        latest[normalize_source_url(event["url"])] = path
    selected = []
    for _, path, event in pending:
        if path != latest[normalize_source_url(event["url"])]:
            event.update(status="superseded", superseded_at=now())
            atomic_json(path,event)
        elif not retryable_only or event.get("status") == "retryable":
            selected.append(path)
    if event_limit:
        selected = selected[:event_limit]
    plans = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {}
        for path in selected:
            event = read_json(path)
            canonical = normalize_source_url(event["url"])
            if limits.paused("processing", event["url"]) and canonical not in probes:
                event.update(status="review")
                atomic_json(path, event)
                plans.append((path, {"status": "review", "review": [{"reason": "retry_limit_reached", "blocking": True}]}))
            else:
                if limits.paused("processing", event["url"]):
                    # A weekly success permits a fresh evidence probe, never a
                    # timestamp-only refresh of the old failed extraction.
                    event.update(status="retryable", retry_reason="fresh_scrape_required",
                                 fresh_evidence_after=limits.get("processing", event["url"])["last_failed_at"])
                    atomic_json(path, event)
                futures[executor.submit(prepare_event,path,snapshot,protections,root)] = path
        for future in as_completed(futures):
            path = futures[future]
            plan = future.result()
            event = read_json(path)
            if event.get("status") == "retryable":
                limits.failed("processing", event["url"], (plan.get("review") or [{}])[-1].get("reason"))
                if limits.paused("processing", event["url"]):
                    event.update(status="review")
                    atomic_json(path, event)
            elif not plan.get("image_retry_reason"):
                limits.succeeded("processing", event["url"])
            plans.append((path,plan))
            atomic_json(root / "sync_progress.json", {"phase":"freezing_plans","prepared_sources":len(plans),
                        "total_sources":len(selected),"last_url":read_json(path)["url"],"updated_at":now()})
    plans.sort(key=lambda row:read_json(row[0]).get("created_at", ""))
    results = []
    for path, plan in plans:
        event = read_json(path)
        if plan["status"] != "ready":
            results.append({"url":event["url"],"status":event.get("status","review"), "review":plan.get("review",[])})
            continue
        if not write_enabled:
            results.append({"url":event["url"],"status":"writes_disabled", "operations":len(plan["operations"]),"review":plan.get("review",[])})
            continue
        receipt = render_pending(plan, protections, root)
        event.update(status=receipt["status"], receipt=receipt)
        if receipt["status"] == "pending_sql" and plan.get("image_retry_reason"):
            event.update(status="retryable",retry_reason=plan["image_retry_reason"],text_receipt=receipt)
            limits.failed("processing",event["url"],plan["image_retry_reason"])
            if limits.paused("processing",event["url"]):event["status"]="review"
        atomic_json(path,event)
        results.append({"url":event["url"],**receipt,"status":event["status"]})
    return {"generated_at":now(),"write_enabled":write_enabled,"sources":results,
            "source_status_counts":dict(Counter(row["status"] for row in results)),
            "previously_verified":verified_batch_results(root, snapshot),
            "protection_count":len(protections["entries"]), "queued_events":len(pending),
            "selected_events":len(plans)}


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=ROOT / "config/firecrawl_monitor_migration.json")
    parser.add_argument("--state-dir",type=Path,default=ROOT / "output/self_hosted_monitor_state")
    parser.add_argument("--control",type=Path)
    parser.add_argument("--database-snapshot",type=Path)
    parser.add_argument("--skip-scrape",action="store_true")
    parser.add_argument("--event-limit",type=int)
    parser.add_argument("--limit",type=int)
    parser.add_argument("--monitor",action="append")
    parser.add_argument("--retry-reviews",action="store_true",help="Re-extract reviewed pages, preserving every prior plan")
    parser.add_argument("--recheck-data-quality-only",action="store_true",help="Capture queued promotion sources; leave membership and service entries to their writers")
    parser.add_argument("--retry-transient-only",action="store_true",help="Process retryable events only; never reopen business reviews")
    args = parser.parse_args()
    root = args.state_dir
    with run_lock(root):
        control = read_json(args.control or root / "price_sync_control.json", {"write_enabled":False})
        if args.retry_reviews:
            for path in (root / "events").glob("*.json"):
                event = read_json(path)
                if event.get("status") == "review" and not event.get("receipt", {}).get("resume_required"):
                    event["status"] = "retryable"
                    atomic_json(path,event)
        reports = []
        browser_cache = {}
        if not args.skip_scrape:
            config = load_config(args.config)
            quality_urls = data_quality_recheck_urls(root)
            snapshot_for_scrape = (
                read_json(args.database_snapshot) if args.database_snapshot else database_snapshot()
            )
            quality_urls.update(expired_active_promotion_urls(snapshot_for_scrape))
            if getattr(args, "recheck_data_quality_only", False):
                config = {"monitors":{"data_quality":{"targets":[{"urls":sorted(quality_urls)}]}}}
            elif quality_urls:
                configured_urls = {url for definition in config["monitors"].values()
                                   for target in definition.get("targets", []) for url in target.get("urls", [])}
                extra = quality_urls - configured_urls
                if extra:
                    config["monitors"]["data_quality"] = {"targets":[{"urls":sorted(extra)}]}
            names = args.monitor or list(config["monitors"])
            retry_urls = retry_failed_scrape_urls(root, config) if args.retry_transient_only else None
            for name in names:
                target_urls = {url for target in config["monitors"][name].get("targets", [])
                               for url in target.get("urls", [])}
                filtered = ({url for url in retry_urls if url in target_urls}
                            if retry_urls is not None else None)
                if filtered is not None and not filtered:
                    continue
                reports.append(run_monitor(config,name,api_url=os.getenv("FIRECRAWL_SELF_HOSTED_API_URL","http://127.0.0.1:3002"),
                    api_key=os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY",""),state_dir=root,
                    timeout=float(os.getenv("FIRECRAWL_HTTP_TIMEOUT_SECS","120")),limit=args.limit,
                    workers=int(os.getenv("FIRECRAWL_MONITOR_WORKERS","3")),send_notifications=False,
                    catchup=not args.retry_transient_only,urls_filter=filtered,scan_images=True,browser_cache=browser_cache))
                atomic_json(root / "monitor_run_report.json", {"generated_at":now(),"reports":reports})
        snapshot = read_json(args.database_snapshot) if args.database_snapshot else database_snapshot()
        atomic_json(root / "database_snapshot.json",snapshot)
        if reports and not args.retry_transient_only:
            expiry_checks = enqueue_expired_campaign_checks(root,snapshot,config)
            quality_checks = enqueue_data_quality_rechecks(root)
        else:
            expiry_checks = 0
            quality_checks = 0
        enabled = control.get("write_enabled") is True
        configured = {url for monitor in load_config(args.config)["monitors"].values()
                      for group in monitor.get("targets", []) for url in group.get("urls", [])}
        RetryLimits(root).refresh_scrapes(configured)
        weekly_probe = bool(reports) and not args.retry_transient_only
        probe_urls = {url for report in reports for url in report.get("successful_urls", [])} if weekly_probe else set()
        report = process_events(root,snapshot,write_enabled=enabled,event_limit=args.event_limit,
                                retryable_only=args.retry_transient_only, retry_probe_urls=probe_urls)
        report["monitors"] = reports
        report["expiry_checks_enqueued"] = expiry_checks
        report["quality_checks_enqueued"] = quality_checks
        report["scrape_failed_pages"] = sum(row["error"] for row in reports)
        report["failure_scope"] = "page_or_source_only"
        report["paused_retries"] = RetryLimits(root).paused_reviews()
        report["run_type"] = ("event_processing" if args.skip_scrape else
                              "failure_retry" if args.retry_transient_only else "full_monitor")
        # Hourly retry reports must not erase the evidence of the weekly run.
        stamp = datetime.fromisoformat(report["generated_at"]).strftime("%Y%m%d_%H%M%S_%f")
        atomic_json(root / "runs" / f"{stamp}_{report['run_type']}.json", report)
        atomic_json(root / "sync_report.json",report)
        atomic_json(root / "review_queue.json",[row for row in report["sources"] if row.get("review") or row["status"] in {"review","retryable"}] + report["paused_retries"])
        print(json.dumps({"report_path":str(root / "sync_report.json"), "write_enabled":enabled,
                          "selected_events":report["selected_events"], "protection_count":report["protection_count"]}))
    return 2 if any(row["error"] for row in reports) or any(row["status"] == "retryable" for row in report["sources"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
