"""Verify retained history, source coverage and the supplemental dry-run contract."""

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "output/monitor_price_sync/dryrun_20261010"
VISUAL = ROOT / "output/monitor_price_sync/visual_review_20261010"
NEW = ROOT / "output/monitor_price_sync/reread_20261011"


def read_csv(path):
    with path.open(encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def read_json(path):
    return json.loads(path.read_text())


def save(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temporary.replace(path)


def main():
    original = read_csv(BASE / "dryrun_changes.csv")
    visual = read_csv(VISUAL / "dryrun_changes_visual_review.csv")
    full = read_csv(NEW / "dryrun_changes_with_reread.csv")
    summary = read_json(VISUAL / "visual_review_summary.json")
    evidence = read_json(VISUAL / "visual_review_evidence.json")
    reread = read_json(NEW / "reread_evidence.json")
    luna = read_json(NEW / "luna_restored_row_findings.json")
    confirmed = read_csv(VISUAL / "host_confirmed_field_changes.csv")
    historical_confirmed = read_csv(VISUAL / "host_confirmed_field_changes_before_user_reread.csv")
    checks = {}

    def require(name, condition):
        checks[name] = bool(condition)
        assert condition, name

    require("1072_original_rows_and_23_fields_retained", len(original) == len(visual) == len(full) == 1072
            and len(original[0]) == 23 and all(
                all(a[key] == b[key] == c[key] for key in a)
                for a, b, c in zip(original, visual, full)))
    require("latest_visual_columns_retained_in_full_addendum", all(
        all(a[key] == b[key] for key in a) for a, b in zip(visual, full)))
    require("189_page_and_179_canonical_source_scope_preserved",
            len(evidence["page_coverage"]) == 189
            and all(all(current[key] == value for key, value in prior.items())
                    for prior, current in zip(reread["summary"]["original_page_coverage"], evidence["page_coverage"]))
            and summary["configured_canonical_sources"] == 179)
    findings = [finding for review in evidence["reviews"] for finding in review["row_findings"]]
    require("178_reviews_and_972_unique_findings", len(evidence["reviews"]) == 178
            and len(findings) == len({f["row_id"] for f in findings}) == 972)
    require("strict_worker_contract_zero_errors", not summary["validation_errors"])
    require("statistics_match_actual_reviews", sum(
        r["complete"] and r["page_status"] == "accessible" for r in evidence["reviews"]
    ) == summary["complete_accessible_sources"] == 171
            and dict(Counter(f["visual_status"] for f in findings)) == summary["pending_row_visual_status"])
    unresolved = read_csv(VISUAL / "unresolved_sources.csv")
    require("remaining_three_incomplete_sources_and_four_original_rows", {r["job_id"] for r in unresolved}
            == set(summary["sources_with_unresolved_visibility"]) == {"VR092", "VR128", "VR131"}
            and sum(int(r["affected_row_count"]) for r in unresolved) == 4)
    policy_jobs = {"VR164", "VR016", "VR105", "VR132"}
    policy_reviews = [r for r in evidence["reviews"] if r["job_id"] in policy_jobs]
    require("404_policy_does_not_claim_page_body_complete", len(policy_reviews) == 4
            and all(r["page_status"] == "not_found" and not r["complete"]
                    and r["source_disposition"] == "confirmed_404_inactive" for r in policy_reviews))
    require("404_has_no_active_matched_database_record", all(
        not json.loads(c["active_promotion_ids"]) and not json.loads(c["active_offer_ids"])
        for c in reread["coverage"] if c["job_id"] in policy_jobs))
    restored = {"VR022", "VR169", "VR007", "VR021"}
    expected_ids = {f["row_id"] for r in evidence["reviews"] if r["job_id"] in restored for f in r["row_findings"]}
    require("new_luna_37_rows_exactly_match_restored_sources", len(luna["findings"]) == len(expected_ids) == 37
            and {f["row_id"] for f in luna["findings"]} == expected_ids)
    viewed = set(luna["actual_viewed_screenshots"])
    require("new_luna_evidence_paths_exist_and_were_actually_viewed", len(viewed) == 17 and all(
        Path(p).is_file() and p in viewed for f in luna["findings"] for p in f["screenshot_refs"]))
    require("original_13_confirmed_fields_preserved", [r for r in confirmed if not r["row_id"].startswith("REREAD_")]
            == historical_confirmed and len(historical_confirmed) == 13)
    require("14_new_field_differences_exactly_link_to_new_report", len(confirmed) == 27 and all(
        any(c["row_id"] == "REREAD_" + r["row_id"] and all(c[k] == r[k]
            for k in ("table", "field", "old_value", "proposed_value", "database_read_at"))
            for c in confirmed) for r in reread["changes"] if r["action"] == "需更新"))
    db = reread["database_snapshot"]
    db_tables = {"clinic_promotions": {str(r["promotion_id"]): r for r in db["promotions"]},
                 "promo_offer_master": {str(r["id"]): r for r in db["offers"]}}
    require("new_update_old_values_match_fresh_read_only_snapshot", all(
        json.loads(r["old_value"]) == db_tables[r["table"]][str(r["offer_id"] or r["promotion_id"])][r["field"]]
        for r in reread["changes"] if r["action"] == "需更新"))
    require("new_candidates_do_not_allocate_database_ids", all(
        not r["offer_id"] and not r["item_id"] for r in reread["changes"] if r["field"] == "candidate_offer"))
    require("dry_run_only_without_schema_or_task_changes", summary["database_writes"] == summary["ddl_calls"]
            == summary["baseline_or_scheduler_mutations"] == reread["summary"]["database_writes"]
            == reread["summary"]["ddl"] == reread["summary"]["baseline_or_scheduler_changes"] == 0)
    now = datetime.now(timezone.utc).isoformat()
    verification = {
        "updated_at": now, "candidate_generated_at": summary["generated_at"],
        "artifact_contract_verdict": "PASS", "overall_goal_verdict": "FIX", "goal_complete": False,
        "scope": "Original 189 pages, 179 canonical sources, 178 pending visual sources and 972 findings retained; 171 complete accessible sources, 4 confirmed native404 resolved by user policy, 3 still incomplete.",
        "checks": checks, "unresolved_sources": sorted(summary["sources_with_unresolved_visibility"]),
        "complete_accessible_sources": 171, "confirmed_source_field_differences": 27,
        "original_database_read_at": "2026-10-10T06:36:02.413Z",
        "supplemental_database_read_at": db["read_at"], "goal_status": "active",
        "blocked_audit_consecutive_goal_turns": 0,
        "worker_terminal_receipt": str(NEW / "luna_restored_progress.json"),
        "browser_control": "user", "task_finish_called": False,
        "database_writes": 0, "ddl_calls": 0, "baseline_or_scheduler_mutations": 0,
    }
    save(VISUAL / "verification.json", verification)
    previous = read_json(VISUAL / "progress_before_user_reread.json")
    save(VISUAL / "progress.json", {
        "updated_at": now, "goal_status": "active", "verdict": "FIX",
        "last_goal_turn_classification": "progress", "previous_goal_turn_classification": "progress",
        "classification_evidence": "User-released browser content yielded four restored sources; a new GPT-6 Luna pixel review supplied 37 exact original findings, merged into full candidate. Four native404 sources resolved by explicit user policy. Old blocked run archived; fresh audit starts at zero.",
        "configured_pages": 189, "configured_canonical_sources": 179, "scope_sources": 178,
        "scope_pending_rows": 972, "sources_with_review": 178, "rows_with_findings": 972,
        "rows_not_reviewed": 0, "complete_accessible_sources": 171,
        "sources_with_unresolved_visibility": sorted(summary["sources_with_unresolved_visibility"]),
        "sources_resolved_by_user_404_policy": sorted(policy_jobs),
        "confirmed_source_field_differences": 27, "workers_active": [],
        "browser_control": "user", "blocked_consecutive_goal_turns": 0,
        "prior_blocked_goal_receipt": previous.get("goal_status_receipt"),
        "database_writes": 0, "schema_changes": 0, "baseline_or_scheduler_mutations": 0,
        "note": "Current work is a read-only dry-run; original scope and old evidence are retained. No goal completion or task.finish while three source bodies remain unproven.",
    })
    print(json.dumps({"verdict": "PASS", "checks": len(checks), "goal_verdict": "FIX", "remaining_sources": 3,
                      "complete_accessible_sources": 171, "confirmed_field_rows": 27}, ensure_ascii=False))


if __name__ == "__main__":
    main()
