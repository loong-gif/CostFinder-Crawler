import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "one-off"))

from candidates_20261020_parallel import load_rows
from utils.firecrawl_scrape_raw_db import canonical_scrape_url
from utils.membership_plan_lookup import normalize_plan_name
from utils.monitor_price_store import atomic_json, now
from utils.supabase_access import fetch_all_rows, load_supabase_client

OUT = ROOT / "one-off/outputs/candidates_20261020"
TABLES = {"clinic_memberships": "plan_id", "clinic_promotions": "promotion_id"}


def read(path):
    return json.loads(path.read_text())


def source_candidate(business_id, source_url, screen_status, latest):
    captured = latest.get((business_id, canonical_scrape_url(source_url)))
    candidate = {"source_url": source_url, "screen_status": screen_status, "status": "capture_missing"}
    if captured:
        candidate.update(capture_path=captured["capture_path"], captured_at=captured["captured_at"])
        path = ROOT / captured["extraction_path"]
        candidate["status"] = "extraction_pending"
        if path.exists():
            extraction = read(path)
            assert extraction["captured_at"] == captured["captured_at"]
            kinds = extraction.get("extractions", {})
            candidate["status"] = "extracted" if all(kind in kinds for kind in ("promotions", "memberships")) else "extraction_incomplete"
            candidate["review_required"] = bool(extraction.get("source_review_required"))
            candidate["explanations"] = {kind: kinds.get(kind, {}).get("explanation") for kind in ("promotions", "memberships")}
    return candidate


def main():
    rows = load_rows()
    assert len(rows) == 760
    businesses = {row["business_id"]: row for row in rows if row.get("master_verified_at")}
    assert len(businesses) == 751
    schema = read(ROOT / "schema/live_schema_snapshot.json")
    plan = read(OUT / "full_plan.json")
    assert plan["mode"] == "dry_run" and plan["database_writes"] == 0
    latest = {}
    for page in plan["pages"]:
        key = (page["business_id"], canonical_scrape_url(page["source_url"]))
        if key not in latest or page["captured_at"] > latest[key]["captured_at"]:
            latest[key] = page

    coverage = []
    for row in rows:
        discovery = row.get("discovery") or {}
        candidates = []
        for page in discovery.get("pages") or []:
            if page.get("status") not in {"valid", "needs_review"}:
                continue
            candidates.append(source_candidate(row["business_id"], page["url"], page["status"], latest))
        candidate_keys = {(row["business_id"], canonical_scrape_url(candidate["source_url"])) for candidate in candidates}
        for page in plan["pages"]:
            if page["input_row"] != row["input_row"] or not Path(page["capture_path"]).name.startswith("scrape_v2_retry_"):
                continue
            key = (row["business_id"], canonical_scrape_url(page["source_url"]))
            if key in candidate_keys:
                continue
            candidates.append(source_candidate(row["business_id"], page["source_url"], "firecrawl_v2_retry", latest))
            candidate_keys.add(key)
        coverage.append({"input_row": row["input_row"], "business_id": row.get("business_id"),
                         "business_name": row["source"]["名称"], "capture_status": row["status"],
                         "capture_reason": row.get("reason"), "candidates": candidates,
                         "failed_pages": [page for page in discovery.get("pages") or [] if page.get("status") == "scrape_failed"],
                         "search_status": discovery.get("search_status")})

    client = load_supabase_client(ROOT)
    current = {table: [item for item in fetch_all_rows(client, table, ",".join(schema[table]), order=f"{key}.asc")
                       if item["business_id"] in businesses] for table, key in TABLES.items()}
    checked_at = now()
    atomic_json(OUT / "source_tables_readback.json", {"checked_at": checked_at, "tables": current})
    outputs = {}
    for table, key in TABLES.items():
        records = []
        for record in plan["records"]:
            if record["table"] != table:
                continue
            identity = (record["business_id"], canonical_scrape_url(record["source_url"]))
            if record["captured_at"] != latest[identity]["captured_at"]:
                continue
            fields = record["insert_fields"]
            assert set(fields) <= set(schema[table])
            existing = [item for item in current[table] if item["business_id"] == record["business_id"]]
            if table == "clinic_memberships":
                matches = [item for item in existing if normalize_plan_name(item["membership_name"]) == normalize_plan_name(fields["membership_name"])
                           and item.get("billing_period") == fields.get("billing_period")]
            else:
                matches = [item for item in existing if canonical_scrape_url(item["source_url"]) == identity[1]]
            action = "review"
            reason = record["reason"]
            differences = {}
            if len(matches) > 1:
                reason = "ambiguous_existing_identity"
            elif matches:
                differences = {field: {"existing": matches[0].get(field), "proposed": value}
                               for field, value in fields.items() if field != "source_url" and matches[0].get(field) != value}
                action = "review" if differences else "existing_equal"
                reason = "existing_record_differs" if differences else "existing_record_equal"
            elif record.get("action") == "reuse" or record["status"] == "duplicate_source":
                action, reason = "duplicate_source", record.get("reason") or "reuse_same_batch"
            elif record["status"] == "ready":
                action = "would_insert"
            records.append({"plan_name": record["plan_name"], "input_row": record["input_row"],
                            "business_id": record["business_id"],
                            "business_name": businesses[record["business_id"]]["source"]["名称"],
                            "action": action, "reason": reason, "evidence_status": record["status"],
                            "evidence_reason": record["reason"], "existing_ids": [item[key] for item in matches],
                            "differences": differences, "insert_fields": fields,
                            "source_evidence": record["original"], "source_url": record["source_url"],
                            "captured_at": record["captured_at"], "capture_path": record["capture_path"],
                            "visibility_path": record["visibility_path"],
                            "visibility_captured_at": record.get("visibility_captured_at"),
                            "reuse_plan_name": record.get("reuse_plan_name")})
        counts = dict(Counter(record["action"] for record in records))
        assert sum(counts.values()) == len(records)
        output = {"generated_at": now(), "mode": "dry_run", "table": table, "database_writes": 0,
                  "plan_generated_at": plan["generated_at"], "database_checked_at": checked_at,
                  "business_count": len({record["business_id"] for record in records}),
                  "record_count": len(records), "action_counts": counts,
                  "existing_rows_in_batch": len(current[table]), "records": records}
        filename = "memberships_dry_run.json" if table == "clinic_memberships" else "promotions_dry_run.json"
        atomic_json(OUT / filename, output)
        outputs[table] = output
    candidate_counts = Counter(page["status"] for row in coverage for page in row["candidates"])
    incomplete = [row["input_row"] for row in coverage if any(page["status"] != "extracted" for page in row["candidates"])]
    capture_pending = [row["input_row"] for row in coverage if row["capture_status"] in {"pending_crawl", "crawling", "waiting_for_firecrawl"}]
    summary = {"generated_at": now(), "database_writes": 0, "schema_changed": False,
               "extraction_coverage_complete": not incomplete and not capture_pending,
               "candidate_counts": dict(candidate_counts), "incomplete_input_rows": incomplete,
               "capture_pending_input_rows": capture_pending, "businesses": coverage}
    atomic_json(OUT / "source_dry_run_coverage.json", summary)
    lines = ["# 候选商家会员与促销 dry-run", "", f"生成时间：{summary['generated_at']}", "",
             f"商家范围：760家；已有 business_id：751家；官网为空跳过：9家。数据库写入：0；Schema未改动。", "",
             f"当前抽取覆盖是否完成：{summary['extraction_coverage_complete']}。尚缺抽取的商家：{len(incomplete)}；采集仍进行中的商家：{len(capture_pending)}。", "",
             "| 目标表 | 涉及商家 | 记录数 | 拟新增 | 已有一致 | 重复来源 | 复核 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for table, output in outputs.items():
        counts = output["action_counts"]
        lines.append(f"| {table} | {output['business_count']} | {output['record_count']} | {counts.get('would_insert', 0)} | {counts.get('existing_equal', 0)} | {counts.get('duplicate_source', 0)} | {counts.get('review', 0)} |")
    lines += ["", "拟新增仅包含当前计划判定 ready 且数据库没有唯一匹配的记录。已有一致记录仍保留独立的证据状态。", "",
              "候选页面抽取状态：" + json.dumps(dict(candidate_counts), ensure_ascii=False), "",
              "逐商家状态与异常见 source_dry_run_coverage.json；原文、价格、权益、日期及数据库差异见两个 dry_run.json。", ""]
    (OUT / "dry_run_summary.md").write_text("\n".join(lines))
    print(json.dumps({"coverage_complete": summary["extraction_coverage_complete"], "candidate_counts": dict(candidate_counts),
                      "tables": {table: output["action_counts"] for table, output in outputs.items()}, "database_writes": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
