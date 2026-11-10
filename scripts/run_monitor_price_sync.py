#!/usr/bin/env python3
"""Promotion monitoring, file evidence/plans, and pending SQL files (no database writes)."""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from scripts.firecrawl_self_hosted_monitor import load_config, run_monitor
from utils.monitor_capture import capture, capture_urls
from utils.monitor_database_snapshot import database_snapshot
from utils.monitor_price_extract import extract_evidence, render_page
from utils.monitor_price_plan import build_plan
from utils.monitor_price_store import atomic_json, digest, manual_protections, now, read_json, run_lock
from utils.monitor_price_writer_plan import normalize_source_url, source_graph
from utils.monitor_pending_sql import plan_write_block, render_pending, settle
from utils.monitor_image_gate import capture_is_current, image_writes_enabled
from utils.monitor_rendered_checks import confirmed_not_found
from utils.monitor_queue import build_config, config_urls
from utils.monitor_price_retry import RetryLimits
from utils.monitor_review_policy import event_status, failure_item, gate_status, needs_review
from utils.monitor_price_visual_receipts import stored_browser
from utils.monitor_event_queue import (
    data_quality_recheck_urls, enqueue_data_quality_rechecks, enqueue_expired_campaign_checks,
    expired_active_promotion_urls, fresh_evidence, verified_batch_results,
)
from utils.monitor_health import service_failures
from utils.monitor_remediation import remediate
from utils.monitor_source_binding import (
    bind_source, check_promotion_source, combine_branch_plans, not_found_plan, redirect_stays_with_business,
    snapshot_business_graph,
)
from utils.pending_sql import pending_dir


def prepare_event(path: Path, snapshot: dict, protections: dict, root: Path) -> dict:
    event = read_json(path)
    directory = root / "sync" / event["event_id"]
    plan_path = directory / "plan.json"
    archived = False
    try:
        # Scope checks precede the cache: old ready plans must not bypass them.
        sources = bind_source(event["url"], snapshot)
        if event.get("retry_reason") == "fresh_scrape_required":
            newest = fresh_evidence(root, event["url"], event["fresh_evidence_after"])
            event["artifact_path"] = str(newest)
            event["artifact_id"] = newest.stem
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
            legacy_image = plan_write_block(cached,pending_dir(root)) in {"legacy_image_evidence_requires_refresh", "breakpoint_coverage_incomplete", "variant_capture_proof_mismatch"}
            policy_changed = (cached.get("image_policy") is not None and
                cached["image_policy"].get("image_write_enabled") != image_writes_enabled(root))
            if not stale and not legacy_image and not policy_changed and event.get("status") != "retryable":
                return cached
            attempt = int(event.get("attempt", 0)) + 1
            atomic_json(directory / f"plan_attempt_{attempt}.json", cached)
            event["attempt"] = attempt
            archived = True
        artifact = read_json(Path(event["artifact_path"]))
        graphs = [source_graph(snapshot_business_graph(snapshot, source["business_id"]), source) for source in sources]
        # business_id 只用于同一页面内规格截图的去重键，各分店共用同一次渲染
        render_business = sources[0]["business_id"]
        browser = stored_browser(root, artifact, event["artifact_path"], directory)
        if (not browser or (not capture_is_current(browser) and not browser.get("verified_capture")) or not browser.get("accessible") or
                event.get("retry_reason") in {"image_capture_incomplete","image_stage_timeout","image_tiles_missing"}):
            browser = render_page(event["url"], directory, business_id=render_business)
        ocr_forced = any(p.get("is_ocr_required") is True for graph in graphs for p in graph["promotions"])
        if ocr_forced and not browser.get("image_review_required"):
            browser = render_page(event["url"], directory, force_ocr=True, business_id=render_business)
        browser["ocr_forced"] = ocr_forced
        browser["dual_cache_path"] = str(root / "visual_cache" / (digest(normalize_source_url(event["url"]))+".json"))
        browser["image_write_enabled"] = image_writes_enabled(root)
        atomic_json(directory / "browser.json",browser)
        not_found = confirmed_not_found(browser)
        if not browser.get("accessible") and not not_found:
            raise RuntimeError("page_not_accessible")
        # 跳到商家自己登记的网站是网站迁移；跳到第三方网站不能把 offer 改绑过去
        if not redirect_stays_with_business(browser["final_url"], event["url"], sources, snapshot):
            raise RuntimeError("source_redirect_requires_review")
        check_promotion_source(browser["final_url"], snapshot)
        if not_found:
            branch_plans = [not_found_plan(source, graph, protections) for source, graph in zip(sources, graphs)]
            atomic_json(directory / "evidence.json",{"url":event["url"],"http_status":404,
                        "visible_not_found":browser["text"],"screenshot_path":browser.get("screenshot_path")})
        else:
            evidence = extract_evidence(artifact, browser, directory, None, snapshot["service_names"])
            branch_plans = [build_plan(source, graph, copy.deepcopy(evidence), protections)
                            for source, graph in zip(sources, graphs)]
        prepared_at = now()
        for branch in branch_plans:
            branch["event_id"] = event["event_id"]
            branch["evidence_path"] = str(directory / "evidence.json")
            branch["prepared_at"] = prepared_at
            if branch.get("image_retry_reason") and branch["status"] != "ready":
                branch["review"].append({"reason":branch["image_retry_reason"],"blocking":False})
        plan = branch_plans[0] if len(branch_plans) == 1 else combine_branch_plans(event["url"], branch_plans)
    except Exception as exc:
        plan = {"schema_version":1,"status":"review", "source":{"url":event["url"]},
                "operations":[],"review":[failure_item(exc)],"event_id":event["event_id"],"prepared_at":now()}
    if plan_path.exists() and not archived:
        attempt = int(event.get("attempt", 0)) + 1
        atomic_json(directory / f"plan_attempt_{attempt}.json", read_json(plan_path))
        event["attempt"] = attempt
    atomic_json(plan_path, plan)
    status, recapture = event_status(plan)
    event.update(status=status, plan_path=str(plan_path))
    if recapture:
        # 证据不足以判定：现有记录不动，等新的抓取（含 ego-lite 渲染）后重新处理
        event.update(retry_reason="fresh_scrape_required", fresh_evidence_after=now())
    atomic_json(path, event)
    return plan


def process_events(root: Path, snapshot: dict, *, event_limit: int | None = None,
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
        if event.get("status") not in {"completed", "superseded", "pending_sql", "excluded"}:
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
                # 暂停小时重试：每日任务抓到新正文后续跑，事件保持待重试
                event.update(status="retryable")
                atomic_json(path, event)
                plans.append((path, {"status": "retryable", "review": [], "hourly_retry_paused": True}))
            else:
                if limits.paused("processing", event["url"]):
                    # A weekly success permits a fresh evidence probe, never a
                    # timestamp-only refresh of the old failed extraction.
                    event.update(status="retryable", retry_reason="fresh_scrape_required",
                                 fresh_evidence_after=limits.get("processing", event["url"])["last_failed_at"])
                    atomic_json(path, event)
                elif event.get("status") == "review" and canonical in probes:
                    # 审查事件随来源按档期的抓取取得新证据，再由监控自身的规则重新判定
                    event.update(status="retryable", retry_reason="fresh_scrape_required",
                                 fresh_evidence_after=read_json(Path(event["plan_path"]))["prepared_at"])
                    atomic_json(path, event)
                futures[executor.submit(prepare_event,path,snapshot,protections,root)] = path
        for future in as_completed(futures):
            path = futures[future]
            plan = future.result()
            event = read_json(path)
            if event.get("status") == "retryable":
                limits.failed("processing", event["url"], (plan.get("review") or [{}])[-1].get("reason"))
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
            results.append({"url":event["url"],"status":event.get("status","review"), "review":plan.get("review",[]),
                            "plan_path":event.get("plan_path")})
            continue
        receipt = render_pending(plan, protections, root)
        event.update(status=receipt["status"], receipt=receipt)
        if receipt["status"] == "review":
            status, recapture = gate_status(receipt["category"])
            event["status"] = status
            if recapture:
                event.update(retry_reason="fresh_scrape_required", fresh_evidence_after=now())
                limits.failed("processing", event["url"], receipt["category"])
        if receipt["status"] == "pending_sql" and plan.get("image_retry_reason"):
            event.update(status="retryable",retry_reason=plan["image_retry_reason"],text_receipt=receipt)
            limits.failed("processing",event["url"],plan["image_retry_reason"])
        atomic_json(path,event)
        results.append({"url":event["url"],**receipt,"status":event["status"]})
    return {"generated_at":now(),"sources":results,
            "source_status_counts":dict(Counter(row["status"] for row in results)),
            "previously_verified":verified_batch_results(root, snapshot),
            "protection_count":len(protections["entries"]), "queued_events":len(pending),
            "selected_events":len(plans)}


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,help="Monitor config file; without it the queue is built from the clinic_promotions and clinic_memberships source URLs")
    parser.add_argument("--state-dir",type=Path,default=ROOT / "output/self_hosted_monitor_state")
    parser.add_argument("--database-snapshot",type=Path)
    parser.add_argument("--skip-scrape",action="store_true")
    parser.add_argument("--capture-only",action="store_true",help="Capture pages and record changes; plan nothing and render no SQL (scheduled crawl observation)")
    parser.add_argument("--event-limit",type=int)
    parser.add_argument("--limit",type=int)
    parser.add_argument("--monitor",action="append")
    parser.add_argument("--recheck-data-quality-only",action="store_true",help="Capture queued promotion sources; leave membership and service entries to their writers")
    parser.add_argument("--retry-transient-only",action="store_true",help="Process retryable events only; never reopen business reviews")
    parser.add_argument("--all-pages",action="store_true",help="Capture every configured page, ignoring the poll schedule")
    args = parser.parse_args()
    root = args.state_dir
    with run_lock(root):
        reports = []
        if not args.skip_scrape:
            snapshot_for_scrape = (
                read_json(args.database_snapshot) if args.database_snapshot else database_snapshot()
            )
            config = load_config(args.config) if args.config else build_config(snapshot_for_scrape, root)
            quality_urls = data_quality_recheck_urls(root)
            quality_urls.update(expired_active_promotion_urls(snapshot_for_scrape))
            if getattr(args, "recheck_data_quality_only", False):
                config = {"monitors":{"data_quality":{"targets":[{"urls":sorted(quality_urls)}]}}}
            elif quality_urls and not args.capture_only:
                configured_urls = {url for definition in config["monitors"].values()
                                   for target in definition.get("targets", []) for url in target.get("urls", [])}
                extra = quality_urls - configured_urls
                if extra:
                    config["monitors"]["data_quality"] = {"targets":[{"urls":sorted(extra)}]}
            # 健康检查失败时本轮的失败记为基础设施故障，不计入来源的站点失败天数
            health = [] if args.capture_only else service_failures()
            # 抓取前已暂停的来源不属于「上一次抓取成功」的来源，不进入基础设施故障的比例
            paused_before = RetryLimits(root).paused_urls()
            started = datetime.now(timezone.utc)
            selected_urls = capture_urls(root, config, quality_urls, retry_only=args.retry_transient_only,
                                         capture_all=args.recheck_data_quality_only or args.all_pages or args.capture_only,
                                         started=started)
            reports = capture(root, config, args.monitor or list(config["monitors"]), selected_urls, limit=args.limit,
                              catchup=not args.retry_transient_only, started=started, run_monitor=run_monitor)
        if args.capture_only:
            # 到期抓取闭环只观察页面变化，计划与待执行 SQL 归每日监控
            return 2 if any(row["error"] for row in reports) else 0
        snapshot = read_json(args.database_snapshot) if args.database_snapshot else database_snapshot()
        atomic_json(root / "database_snapshot.json",snapshot)
        if reports and not args.retry_transient_only:
            expiry_checks = enqueue_expired_campaign_checks(root,snapshot,config)
            quality_checks = enqueue_data_quality_rechecks(root)
        else:
            expiry_checks = 0
            quality_checks = 0
        queue = load_config(args.config) if args.config else build_config(snapshot, root)
        RetryLimits(root).refresh_scrapes(set(config_urls(queue)))
        weekly_probe = bool(reports) and not args.retry_transient_only
        probe_urls = {url for report in reports for url in report.get("successful_urls", [])} if weekly_probe else set()
        report = process_events(root,snapshot,event_limit=args.event_limit,
                                retryable_only=args.retry_transient_only, retry_probe_urls=probe_urls)
        report["monitors"] = reports
        report["expiry_checks_enqueued"] = expiry_checks
        report["quality_checks_enqueued"] = quality_checks
        report["scrape_failed_pages"] = sum(row["error"] for row in reports)
        report["failure_scope"] = "page_or_source_only"
        report["paused_retries"] = RetryLimits(root).paused_sources()
        if reports:
            report["remediation"] = remediate(root, reports=reports, sources=report["sources"], health=health,
                                              at=started, paused=paused_before)
        report["run_type"] = ("event_processing" if args.skip_scrape else
                              "failure_retry" if args.retry_transient_only else "full_monitor")
        # Hourly retry reports must not erase the evidence of the weekly run.
        stamp = datetime.fromisoformat(report["generated_at"]).strftime("%Y%m%d_%H%M%S_%f")
        atomic_json(root / "runs" / f"{stamp}_{report['run_type']}.json", report)
        atomic_json(root / "sync_report.json",report)
        # 审查清单只收证据无法判定的来源；待重试与暂停小时重试的来源由任务自己续跑
        atomic_json(root / "review_queue.json",[row for row in report["sources"]
                    if row["status"] == "review" or any(needs_review(item) for item in row.get("review", []))])
        print(json.dumps({"report_path":str(root / "sync_report.json"),
                          "selected_events":report["selected_events"], "protection_count":report["protection_count"]}))
    return 2 if any(row["error"] for row in reports) or any(row["status"] == "retryable" for row in report["sources"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
