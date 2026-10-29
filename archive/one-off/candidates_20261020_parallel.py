import argparse
import csv
import json
import os
import re
import sys
from argparse import Namespace
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "one-off"))

from candidates_20261020_run import ServerFirecrawl, norm
from scripts.discover_new_promo_pages import SearchHealth, discover_business
from utils.batch_gate_checks import linked_query
from utils.discovery_logic import website_host
from utils.monitor_price_store import atomic_json, now, run_lock
from utils.supabase_access import fetch_all_rows, load_supabase_client

OUT = ROOT / "one-off/outputs/candidates_20261020"
RANGES = {1: (1, 253), 2: (254, 506), 3: (507, 760)}


def discovery_row_status(result):
    """Script-only page screen outcome for one discover_business() result."""
    pages = result.get("pages") or []
    err = str(result.get("error") or "").strip()
    if any(page.get("status") == "scrape_failed" for page in pages):
        return "crawl_failed", err[:200] if err else "部分页面抓取失败"
    if err and ("HTTPError: 500" in err or (err.startswith("ConnectionError:") and "192.168.1.68" in err)):
        return "crawl_failed", err[:200]
    if not pages:
        if result.get("search_status") in {"error", "skipped_engines_down"}:
            return "crawl_failed", str(result.get("search_error") or "搜索未完成，无法确认页面覆盖")
        if err:
            return "crawl_failed", err[:200]
        return "no_promotion_page", "现有发现流程未找到包含有效价格或优惠证据的页面"
    if any(page.get("status") in {"valid", "needs_review"} for page in pages):
        return "pending_extraction", "等待结构化抽取与当前可见性核验"
    return "no_promotion_page", "现有发现流程未找到包含有效价格或优惠证据的页面"


def should_retry_discovery(row):
    discovery = row.get("discovery") or {}
    err = str(discovery.get("error") or "")
    if not discovery.get("pages") and (err or discovery.get("search_status") in {"error", "skipped_engines_down"}):
        return True
    if err.startswith("ConnectionError:") and "192.168.1.68" in err:
        return True
    if "HTTPError: 500" in err:
        return True
    if any(page.get("status") == "scrape_failed" for page in discovery.get("pages") or []):
        return True
    if row.get("status") == "crawling" and not row.get("discovery_completed_at"):
        return True
    return False


def archive_discovery_for_retry(row):
    if not row.get("discovery"):
        return
    row.setdefault("discovery_history", []).append({"completed_at": row.get("discovery_completed_at"), "result": row.pop("discovery")})
    row.pop("discovery_completed_at", None)


def prepare_row_for_run(row):
    if row["status"] in {"skipped", "review"}:
        return "skip"
    if should_retry_discovery(row):
        archive_discovery_for_retry(row)
        row.update(status="pending_crawl", reason=row.get("reason") or "等待补抓失败页面")
        return "crawl"
    if row.get("discovery") and row.get("discovery_completed_at"):
        status, reason = discovery_row_status(row["discovery"])
        row.update(status=status, reason=reason)
        return "done"
    if row.get("discovery_completed_at"):
        return "done"
    if row["status"] == "crawling":
        row.update(status="pending_crawl", reason="上次采集中断，续跑")
    return "crawl"


def build_screen_summary(rows):
    screened = []
    for row in rows:
        discovery = row.get("discovery") or {}
        pages = discovery.get("pages") or []
        passed = [{"url": page["url"], "status": page["status"], "kind": page.get("kind"), "evidence": page.get("evidence") or []}
                  for page in pages if page.get("status") in {"valid", "needs_review"}]
        failed = [{"url": page["url"], "error": page.get("error")} for page in pages if page.get("status") == "scrape_failed"]
        screened.append({
            "input_row": row["input_row"],
            "business_id": row.get("business_id"),
            "website": (row.get("source") or {}).get("官网"),
            "status": row.get("status"),
            "search_status": discovery.get("search_status"),
            "passed_pages": passed,
            "failed_pages": failed,
        })
    return {
        "checked_at": now(),
        "statuses": dict(Counter(row.get("status") for row in rows)),
        "passed_businesses": sum(1 for item in screened if item["passed_pages"]),
        "businesses": screened,
    }


def read(path):
    return json.loads(path.read_text())


def load_rows():
    rows = read(OUT / "business_status.json")
    with (ROOT / "one-off/inputs/costfinder_candidates_medspa_plastic.csv").open(encoding="utf-8-sig", newline="") as handle:
        source = list(csv.DictReader(handle))
    assert len(source) == len(rows) == 760
    assert len({row["google place_id"] for row in source}) == 760
    for index, (row, original) in enumerate(zip(rows, source), 1):
        assert row["source"] == original and row["input_row"] == index, "input_checkpoint_changed"
    for batch in RANGES:
        path = OUT / "batches" / f"batch_{batch}" / "status.json"
        if path.exists():
            for row in read(path):
                assert rows[row["input_row"] - 1]["source"] == row["source"]
                rows[row["input_row"] - 1] = row
    return rows


def street(value):
    parts = str(value or "").split()
    return parts[0].lower() if parts and re.search(r"\d", parts[0]) else ""


def phone(value):
    return re.sub(r"\D", "", str(value or ""))[-10:]


def prepare(apply):
    with run_lock(OUT):
        rows = load_rows()
        schema = read(ROOT / "schema/live_schema_snapshot.json")
        client = load_supabase_client(ROOT)
        client.assert_write_project()
        masters = fetch_all_rows(client, "master_business_info", ",".join(schema["master_business_info"]), order="business_id.asc")
        phones = {record["business_id"]: record["phone"] for record in linked_query("select business_id,phone from master_business_info order by business_id", root=ROOT)}
        candidate_phones = {row["source"]["google place_id"]: row["source"]["电话"] for row in rows}
        for master in masters:
            master["phone"] = phones.get(master["business_id"]) or candidate_phones.get(master.get("google_place_id"))
        initial = {row["business_id"] for row in read(OUT / "master_before.json")}
        pending = []
        for row in rows:
            source = row["source"]
            if not source["官网"].strip():
                row.update(status="skipped", reason="官网为空")
                continue
            place = [m for m in masters if m.get("google_place_id") == source["google place_id"]]
            domain = [m for m in masters if website_host(m.get("website")) == website_host(source["官网"]) and street(m.get("address")) and street(m["address"]) == street(source["地址"])]
            exact = [m for m in masters if norm(m.get("name")) == norm(source["名称"]) and norm(m.get("address")) == norm(source["地址"])]
            telephone = [m for m in masters if phone(source["电话"]) and phone(m.get("phone")) == phone(source["电话"])]
            matches = place or domain or exact
            method = "place_id" if place else "domain_street_number" if domain else "name_address" if exact else None
            if not matches and telephone:
                same_site_new_address = all(website_host(m.get("website")) == website_host(source["官网"]) and street(m.get("address")) and street(source["地址"]) and street(m["address"]) != street(source["地址"]) for m in telephone)
                if not same_site_new_address:
                    matches, method = telephone, "phone"
            if len(matches) > 1:
                row.update(status="review", reason="商家身份存在多个匹配", matches=[m.get("business_id") or {"input_row": m["candidate_row"]} for m in matches])
                continue
            if matches:
                master = matches[0]
                if master.get("business_id"):
                    row.update(business_id=master["business_id"], master_action="matched" if master["business_id"] in initial else "inserted", match_method=method, master_verified_at=now())
                    if row["status"] == "pending":
                        row.update(status="pending_crawl", reason="商家已匹配，等待采集")
                else:
                    row.update(master_action="matched_candidate", master_reference_row=master["candidate_row"], match_method=method)
                continue
            payload = {"name": source["名称"], "address": source["地址"], "city": source["城市"], "review_count": int(source["评论数"]), "score": float(source["评分"]), "category": source["谷歌品类"], "website": source["官网"], "google_place_id": source["google place_id"]}
            assert set(payload) <= set(schema["master_business_info"])
            assert payload["name"] and payload["address"] and payload["google_place_id"]
            row.update(master_action="insert", master_intent=payload)
            pending.append(row)
            masters.append({**payload, "phone": source["电话"], "candidate_row": row["input_row"]})
        atomic_json(OUT / "full_master_plan.json", {"prepared_at": now(), "insert_rows": [row["input_row"] for row in pending], "rows": rows})
        print(json.dumps({"phase": "master_plan", "insert": len(pending), "statuses": dict(Counter(row["status"] for row in rows)), "apply": apply}), flush=True)
        if not apply:
            return
        for offset in range(0, len(pending), 25):
            group = pending[offset:offset + 25]
            atomic_json(OUT / "master_insert_intent.json", {"started_at": now(), "rows": [row["input_row"] for row in group], "payloads": [row["master_intent"] for row in group]})
            created = client.insert_rows("master_business_info", [row["master_intent"] for row in group])
            assert len(created) == len(group), "master_insert_response_incomplete"
            by_place = {master["google_place_id"]: master for master in created}
            for row in group:
                master = by_place[row["source"]["google place_id"]]
                assert all(master[key] == value for key, value in row["master_intent"].items()), "master_insert_mismatch"
                row.update(business_id=master["business_id"], master_action="inserted", status="pending_crawl", reason="商家已写入，等待回读")
            ids = ",".join(str(row["business_id"]) for row in group)
            actual = client.fetch_rows("master_business_info", ",".join(schema["master_business_info"]), filters={"business_id": f"in.({ids})"})
            by_id = {master["business_id"]: master for master in actual}
            for row in group:
                assert all(by_id[row["business_id"]][key] == value for key, value in row["master_intent"].items()), "master_independent_readback_mismatch"
                row.update(master_verified_at=now(), reason="商家已写入并回读，等待采集")
            atomic_json(OUT / "business_status.json", rows)
            print(json.dumps({"phase": "masters_verified", "this_group": len(group), "total_new_verified": offset + len(group)}), flush=True)
        for row in rows:
            if row.get("master_action") == "matched_candidate":
                reference = rows[row["master_reference_row"] - 1]
                assert reference.get("master_verified_at")
                row.update(business_id=reference["business_id"], master_verified_at=reference["master_verified_at"], status="pending_crawl", reason="匹配本批次同一商家，等待采集")
        for row in rows:
            if row["status"] not in {"skipped", "review"}:
                assert row.get("business_id") and row.get("master_verified_at")
        atomic_json(OUT / "business_status.json", rows)
        for batch, (start, end) in RANGES.items():
            selected = [row for row in rows if start <= row["input_row"] <= end]
            assert len(selected) == end - start + 1
            atomic_json(OUT / "batches" / f"batch_{batch}" / "status.json", selected)
        atomic_json(OUT / "parallel_ready.json", {"prepared_at": now(), "businesses": 760, "batches": RANGES, "api_url": "http://192.168.1.68:3002", "max_concurrency": 3})


def run(batch, after_row=0):
    assert (OUT / "parallel_ready.json").exists(), "prepare_all_businesses_first"
    directory = OUT / "batches" / f"batch_{batch}"
    os.environ["COSTFINDER_RAW_DIR"] = str(directory / "raw")
    with run_lock(directory):
        rows = read(directory / "status.json")
        for row in rows:
            prepare_row_for_run(row)
        atomic_json(directory / "status.json", rows)
        client = load_supabase_client(ROOT)
        args = Namespace(sources="map,search", map_limit=500, max_candidates=24, allow_blog=False, search_limit=10, search_health=SearchHealth(), search_client=None, search_retry_delay=0, max_pages=24, hops=1, max_hop_links=8)
        for row in rows:
            if row["input_row"] <= after_row:
                continue
            if prepare_row_for_run(row) in {"skip", "done"}:
                continue
            assert row.get("master_verified_at")
            row.update(status="crawling", last_attempt_at=now())
            atomic_json(directory / "status.json", rows)
            print(json.dumps({"batch": batch, "row": row["input_row"], "status": "crawling"}), flush=True)
            source = row["source"]
            result = discover_business(ServerFirecrawl(client, row, reuse_captures=True), {"business_id": row["business_id"], "name": source["名称"], "city": source["城市"], "website": source["官网"]}, set(), args)
            if str(result.get("error") or "").startswith("ConnectionError:") and "192.168.1.68" in result["error"]:
                row.update(status="waiting_for_firecrawl", reason=result["error"], transport_failure_at=now())
                atomic_json(directory / "status.json", rows)
                raise RuntimeError("firecrawl_transport_unavailable")
            row.update(discovery=result, discovery_completed_at=now())
            status, reason = discovery_row_status(result)
            row.update(status=status, reason=reason)
            atomic_json(directory / "status.json", rows)
            print(json.dumps({"batch": batch, "row": row["input_row"], "status": row["status"], "pages": len(result["pages"])}), flush=True)
        atomic_json(directory / "capture_result.json", {"completed_at": now(), "statuses": dict(Counter(row["status"] for row in rows)), "rows": len(rows)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "run", "summary"])
    parser.add_argument("--batch", type=int, choices=RANGES)
    parser.add_argument("--after-row", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.apply)
    elif args.command == "run":
        assert args.batch, "batch_required"
        run(args.batch, after_row=args.after_row)
    else:
        rows = load_rows()
        for row in rows:
            if row.get("discovery") and row.get("discovery_completed_at"):
                status, reason = discovery_row_status(row["discovery"])
                row.update(status=status, reason=reason)
        summary = build_screen_summary(rows)
        atomic_json(OUT / "screen_summary.json", summary)
        atomic_json(OUT / "parallel_summary.json", {"checked_at": now(), "statuses": dict(Counter(row["status"] for row in rows)), "rows": rows})
        print(json.dumps({"statuses": summary["statuses"], "passed_businesses": summary["passed_businesses"], "masters_verified": sum(bool(row.get("master_verified_at")) for row in rows)}))


if __name__ == "__main__":
    main()
