from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from utils.monitor_capture import capture_urls
from utils.monitor_price_store import atomic_json
from utils.monitor_price_writer_plan import normalize_source_url
from utils.monitor_remediation import remediate

SITE = "https://blocked.test/specials"
OTHER = "https://ok.test/specials"
START = datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)


def monitor(errors=(), successes=()):
    return {"total_pages": len(errors) + len(successes), "error": len(errors),
            "page_errors": [{"url": url, "error": reason} for url, reason in errors],
            "successful_urls": list(successes)}


def healthy_bulk(n):
    # 让单个来源的失败低于 30% 的批量故障阈值
    return [f"https://fine{i}.test/specials" for i in range(n)]


def run(root, day, *, errors=(), successes=(), sources=(), health=()):
    return remediate(root, reports=[monitor(errors, [*successes, *healthy_bulk(9)])], sources=list(sources),
                     health=list(health), at=START + timedelta(days=day), paused=set())


def log(root):
    return [json.loads(line) for line in (root / "remediation_log.jsonl").read_text().splitlines()]


def removals(root):
    return json.loads((root / "monitor_removals.json").read_text())["entries"]


def test_site_failure_on_fourteen_distinct_dates_removes_the_source(tmp_path):
    for day in range(13):
        # 同一天的第二次失败不增加天数
        run(tmp_path, day, errors=[(SITE, "ego_scrape_failed")])
        summary = run(tmp_path, day, errors=[(SITE, "ego_scrape_failed")])
        assert summary["removed"] == []
    assert summary["site_failures"] == [{"url": SITE, "days": 13, "reason": "ego_scrape_failed"}]
    summary = run(tmp_path, 13, errors=[(SITE, "ego_scrape_failed")])
    assert summary["removed"] == [SITE]
    entry, = removals(tmp_path)
    assert entry == {"url": SITE, "reason": "ego_scrape_failed", "removed_at": (START + timedelta(days=13)).isoformat(),
                     "next_recheck_at": (START + timedelta(days=43)).isoformat()}
    removed, = [row for row in log(tmp_path) if row["action"] == "removed"]
    assert removed["classification"] == "site" and removed["stage"] == "scrape" and len(removed["evidence"]["dates"]) == 14


def test_success_resets_the_site_failure_days(tmp_path):
    for day in range(10):
        run(tmp_path, day, errors=[(SITE, "firecrawl_empty_markdown")])
    run(tmp_path, 10, successes=[SITE])
    for day in range(11, 24):
        summary = run(tmp_path, day, errors=[(SITE, "ego_output_incomplete: 2/5 chunks")])
    assert summary["removed"] == [] and summary["site_failures"][0]["days"] == 13


def test_processing_page_not_accessible_counts_and_pipeline_reasons_never_do(tmp_path):
    inaccessible = [{"url": SITE, "status": "retryable", "review": [{"reason": "page_not_accessible"}]},
                    {"url": OTHER, "status": "retryable", "review": [{"reason": "image_capture_incomplete"}]}]
    for day in range(14):
        summary = run(tmp_path, day, successes=[SITE, OTHER], sources=inaccessible,
                      errors=[("https://net.test/specials", "ConnectionError")])
    assert summary["removed"] == [SITE]
    assert [row["url"] for row in log(tmp_path) if row["action"] == "removed"] == [SITE]


def test_infrastructure_faults_do_not_count_toward_any_source(tmp_path):
    for day in range(20):
        # 每天都有一批原本正常的来源同时失败：6/15 页同一原因（≥30%）
        bulk = [(f"https://down{day}-{i}.test/specials", "ego_scrape_failed") for i in range(5)]
        summary = run(tmp_path, day, errors=[(SITE, "ego_scrape_failed"), *bulk])
    assert summary["removed"] == [] and summary["site_failures"] == []
    assert summary["infra_faults"] == [{"reason": "ego_scrape_failed", "count": 6, "total": 15}]
    summary = run(tmp_path, 21, errors=[(SITE, "page_not_accessible")],
                  health=[{"service": "ocr", "error": "HTTP 503"}])
    assert summary["site_failures"] == []
    assert summary["infra_faults"] == [{"service": "ocr", "error": "HTTP 503"}]
    faults = [row for row in log(tmp_path) if row["action"] == "infra_fault"]
    assert len(faults) == 21 and faults[-1]["classification"] == "infra" and faults[-1]["url"] is None


def test_share_of_failures_is_measured_on_sources_whose_previous_capture_succeeded(tmp_path):
    failing = [f"https://blocked{i}.test/specials" for i in range(66)]
    healthy = [f"https://healthy{i}.test/specials" for i in range(110)]
    atomic_json(tmp_path / "site_failure_days.json", {"schema_version": 1, "sources": {
        normalize_source_url(url): {"url": url, "dates": ["2026-10-31"], "reason": "page_not_accessible"}
        for url in failing}})
    inaccessible = [{"url": url, "status": "retryable", "review": [{"reason": "page_not_accessible"}]}
                    for url in failing]
    # 暂停的来源与到期复查的来源也是上一次失败的来源，不进入比例
    paused = [f"https://paused{i}.test/specials" for i in range(40)]
    summary = remediate(tmp_path, reports=[monitor([(url, "ConnectionError") for url in paused],
                                                   [*failing, *healthy])],
                        sources=inaccessible, health=[], at=START, paused=set(paused))
    assert summary["infra_faults"] == []
    assert {row["days"] for row in summary["site_failures"]} == {2} and len(summary["site_failures"]) == 66
    broken = [(url, "ConnectionError") for url in healthy[:40]]
    summary = remediate(tmp_path, reports=[monitor(broken, [*failing, *healthy[40:]])], sources=inaccessible,
                        health=[], at=START + timedelta(days=1), paused=set())
    assert summary["infra_faults"] == [{"reason": "ConnectionError", "count": 40, "total": 110}]
    assert {row["days"] for row in summary["site_failures"]} == {3}


def test_credentials_rejected_is_an_infrastructure_fault(tmp_path):
    rejected = [{"url": OTHER, "status": "retryable", "review": [{"reason": "extraction_credentials_rejected"}]}]
    summary = run(tmp_path, 0, successes=[OTHER], sources=rejected)
    assert summary["infra_faults"] == [{"reason": "extraction_credentials_rejected", "count": 1, "total": 10}]
    assert summary["awaiting_human"] == []


def remove(root, at):
    atomic_json(root / "monitor_removals.json", {"schema_version": 1, "entries": [
        {"url": SITE, "reason": "ego_scrape_failed", "removed_at": at.isoformat(),
         "next_recheck_at": (at + timedelta(days=30)).isoformat()}]})


def configured(root):
    return {"schema_version": 1, "monitors": {"daily_01": {"targets": [{"urls": [SITE, OTHER]}]}}}


def test_capture_excludes_removed_sources_until_their_recheck_is_due(tmp_path):
    remove(tmp_path, START)
    config = configured(tmp_path)
    selected = capture_urls(tmp_path, config, {SITE}, retry_only=False, capture_all=False, started=START)
    assert SITE not in selected and OTHER in selected
    due = capture_urls(tmp_path, config, set(), retry_only=False, capture_all=False,
                       started=START + timedelta(days=30))
    assert SITE in due
    atomic_json(tmp_path / "failed_evidence/1.json", {"url": SITE, "captured_at": START.isoformat(), "error": "x"})
    assert capture_urls(tmp_path, config, set(), retry_only=True, capture_all=False, started=START) == set()


def test_failed_recheck_waits_thirty_more_days_and_a_successful_one_restores(tmp_path):
    remove(tmp_path, START)
    summary = run(tmp_path, 30, errors=[(SITE, "ego_scrape_failed")])
    assert summary["recheck_failed"] == [SITE] and summary["site_failures"] == []
    assert removals(tmp_path)[0]["next_recheck_at"] == (START + timedelta(days=60)).isoformat()
    # 流水线原因的失败不说明站点状态：名单不变，下一次运行再复查
    run(tmp_path, 60, errors=[(SITE, "ConnectionError")])
    assert removals(tmp_path)[0]["next_recheck_at"] == (START + timedelta(days=60)).isoformat()
    summary = run(tmp_path, 61, successes=[SITE])
    assert summary["restored"] == [SITE] and removals(tmp_path) == []
    assert [row["action"] for row in log(tmp_path)] == ["recheck_failed", "restored"]


def test_infrastructure_fault_leaves_a_due_recheck_in_place(tmp_path):
    remove(tmp_path, START)
    summary = run(tmp_path, 30, errors=[(SITE, "ego_scrape_failed")], health=[{"service": "ego-lite", "error": "x"}])
    assert summary["recheck_failed"] == []
    assert removals(tmp_path)[0]["next_recheck_at"] == (START + timedelta(days=30)).isoformat()


def review_row(*reasons, url=OTHER):
    return {"url": url, "status": "review", "plan_path": "sync/e1/plan.json",
            "review": [{"reason": reason} for reason in reasons]}


def test_human_review_is_archived_once_per_source_and_reason_until_the_outcome_changes(tmp_path):
    summary = run(tmp_path, 0, successes=[OTHER], sources=[review_row("ambiguous_offer_match", "KeyError")])
    assert summary["awaiting_human"] == [{"url": OTHER, "reasons": ["ambiguous_offer_match"]}]
    assert run(tmp_path, 1, successes=[OTHER], sources=[review_row("ambiguous_offer_match")])["awaiting_human"] == []
    changed = run(tmp_path, 2, successes=[OTHER], sources=[review_row("source_redirect_requires_review")])
    assert changed["awaiting_human"] == [{"url": OTHER, "reasons": ["source_redirect_requires_review"]}]
    resolved = run(tmp_path, 3, successes=[OTHER], sources=[{"url": OTHER, "status": "pending_sql"}])
    assert resolved["resolved"] == [OTHER]
    assert run(tmp_path, 4, successes=[OTHER], sources=[{"url": OTHER, "status": "completed"}])["resolved"] == []
    awaiting = [row for row in log(tmp_path) if row["action"] == "awaiting_human"]
    assert [row["reason"] for row in awaiting] == ["ambiguous_offer_match", "source_redirect_requires_review"]
    assert awaiting[0]["classification"] == "human_review" and awaiting[0]["evidence"] == {"plan": "sync/e1/plan.json"}


def test_review_not_recaptured_in_this_run_and_other_review_reasons_are_not_archived(tmp_path):
    summary = run(tmp_path, 0, sources=[review_row("protected_row_or_cascade"), review_row("KeyError", url=SITE)],
                  successes=[SITE])
    assert summary["awaiting_human"] == []
    summary = run(tmp_path, 1, successes=[OTHER], sources=[review_row("protected_row_or_cascade")])
    assert summary["awaiting_human"] == [{"url": OTHER, "reasons": ["protected_row_or_cascade"]}]
