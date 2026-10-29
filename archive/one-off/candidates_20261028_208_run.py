import argparse
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

from scripts.discover_new_promo_pages import Firecrawl, SearchHealth, discover_business
from utils.discovery_logic import website_host
from utils.firecrawl_scrape_raw_db import save_scrape_response, scrape_request_fingerprint
from utils.firecrawl_search_raw_db import save_search_response, search_request_fingerprint
from utils.monitor_price_store import atomic_json, now, run_lock
from utils.supabase_access import fetch_all_rows, load_supabase_client

OUT = ROOT / "one-off/outputs/candidates_20261028_208"
os.environ["COSTFINDER_RAW_DIR"] = str(OUT / "raw")


def norm(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def pilot(rows):
    target = OUT / "pilot_rows.json"
    if target.exists():
        return json.loads(target.read_text())
    eligible = [r for r in rows if r["status"] != "skipped"]
    selected = [eligible[i * len(eligible) // 20]["input_row"] for i in range(20)]
    assert len(set(selected)) == 20
    atomic_json(target, selected)
    return selected


def seed(rows, selected, client):
    masters = fetch_all_rows(client, "master_business_info", "business_id,name,address,city,website,google_place_id", order="business_id.asc")
    before = {r["business_id"] for r in json.loads((OUT / "master_before.json").read_text())}
    schema = json.loads((ROOT / "schema/live_schema_snapshot.json").read_text())
    for row in rows:
        if row["input_row"] not in selected:
            continue
        source = row["source"]
        matches = [m for m in masters if m.get("google_place_id") == source["google place_id"]]
        if not matches:
            matches = [m for m in masters if (website_host(m.get("website")) == website_host(source["官网"]) and str(m.get("address") or "").split()[:1] == source["地址"].split()[:1]) or (norm(m.get("name")) == norm(source["名称"]) and norm(m.get("address")) == norm(source["地址"]))]
        assert len(matches) <= 1, "ambiguous_master_identity"
        if matches:
            master = matches[0]
            row.update(business_id=master["business_id"], master_action="matched" if master["business_id"] in before else "inserted")
        else:
            assert row["master_action"] == "insert", "preflight_review_required"
            payload = {"name": source["名称"], "address": source["地址"], "city": source["城市"], "review_count": int(source["评论数"]), "score": float(source["评分"]), "category": source["谷歌品类"], "website": source["官网"], "google_place_id": source["google place_id"]}
            assert set(payload) <= set(schema["master_business_info"])
            row.update(master_intent=payload, master_started_at=now())
            atomic_json(OUT / "business_status.json", rows)
            created = client.insert_rows("master_business_info", [payload])
            assert len(created) == 1
            master = created[0]
            assert all(master[k] == value for k, value in payload.items())
            masters.append(master)
            row.update(business_id=master["business_id"], master_action="inserted")
        actual = client.fetch_rows("master_business_info", "business_id,google_place_id,address,name", filters={"business_id": f"eq.{row['business_id']}"})
        assert len(actual) == 1 and actual[0]["google_place_id"] == source["google place_id"], "master_readback_mismatch"
        row["master_verified_at"] = now()
        if row["status"] == "pending":
            row.update(status="pending_crawl", reason="商家已写入并回读，等待试跑抓取")
        atomic_json(OUT / "business_status.json", rows)
    print(json.dumps({"pilot_seeded": len(selected), "master_actions": dict(Counter(r["master_action"] for r in rows if r["input_row"] in selected))}))


class ServerFirecrawl(Firecrawl):
    def __init__(self, client, source, reuse_captures=False):
        super().__init__("http://192.168.1.68:3002", api_key=os.getenv("FIRECRAWL_SELF_HOSTED_API_KEY", ""), timeout=30, search_interval=5)
        self.client = client
        self.source = source
        self.captures = []
        if reuse_captures:
            self.captures = [json.loads(path.read_text()) for path in (OUT / "requests" / str(source["input_row"])).glob("*.json")]

    def _post(self, path, body):
        matches = [capture for capture in self.captures if capture.get("api_url") == self.api_url and capture["path"] == path and capture["request"] == body and capture["response"].get("success", True)
                   and (path != "/v1/search" or capture["response"].get("data"))]
        if matches:
            return max(matches, key=lambda capture: capture["captured_at"])["response"]
        response = super()._post(path, body)
        assert response.get("success", True), "firecrawl_unsuccessful_response"
        directory = OUT / "requests" / str(self.source["input_row"])
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / f"{path.rsplit('/', 1)[1]}_{len(list(directory.glob('*.json'))):03d}.json", {"captured_at": now(), "api_url": self.api_url, "path": path, "request": body, "response": response})
        if path == "/v1/search":
            website = self.source["source"]["官网"]
            fingerprint = search_request_fingerprint(website=website, domain=website_host(website), query=body["query"])
            save_search_response(self.client, fingerprint, response, search_query=body["query"])
        elif path == "/v1/scrape":
            save_scrape_response(self.client, scrape_request_fingerprint(body["url"], formats=body["formats"], only_main_content=body.get("onlyMainContent", True)), body["url"], response)
        return response


def crawl(rows, selected, client, retry_failed=False):
    args = Namespace(sources="map,search", map_limit=500, max_candidates=24, allow_blog=False, search_limit=10, search_health=SearchHealth(), search_client=None, search_retry_delay=0, max_pages=24, hops=1, max_hop_links=8)
    for row in rows:
        if row["input_row"] not in selected or (retry_failed and row["status"] != "crawl_failed") or (not retry_failed and row.get("discovery_completed_at")):
            continue
        assert row.get("master_verified_at"), "seed_master_first"
        fc = ServerFirecrawl(client, row, reuse_captures=retry_failed)
        source = row["source"]
        print(json.dumps({"row": row["input_row"], "business_id": row["business_id"], "status": "crawling", "api_url": fc.api_url}), flush=True)
        result = discover_business(fc, {"business_id": row["business_id"], "name": source["名称"], "city": source["城市"], "website": source["官网"]}, set(), args)
        if row.get("discovery"):
            row.setdefault("discovery_history", []).append({"completed_at": row["discovery_completed_at"], "result": row["discovery"]})
        row["discovery"] = result
        row["discovery_completed_at"] = now()
        if result.get("error") or any(p["status"] == "scrape_failed" for p in result["pages"]):
            row.update(status="crawl_failed", reason=result.get("error") or "部分页面抓取失败")
        elif any(p["status"] in {"valid", "needs_review"} for p in result["pages"]):
            row.update(status="pending_extraction", reason="等待结构化抽取与当前可见性核验")
        elif result.get("search_status") in {"error", "skipped_engines_down"}:
            row.update(status="crawl_failed", reason="搜索未完成，无法确认页面覆盖")
        else:
            row.update(status="no_promotion_page", reason="现有发现流程未找到包含有效价格或优惠证据的页面")
        atomic_json(OUT / "business_status.json", rows)
        print(json.dumps({"row": row["input_row"], "business_id": row["business_id"], "status": row["status"], "pages": len(result["pages"])}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed-pilot", "crawl-pilot", "retry-pilot"])
    args = parser.parse_args()
    with run_lock(OUT):
        rows = json.loads((OUT / "business_status.json").read_text())
        selected = pilot(rows)
        client = load_supabase_client(ROOT)
        client.assert_write_project()
        if args.command == "seed-pilot":
            seed(rows, selected, client)
        else:
            crawl(rows, selected, client, retry_failed=args.command == "retry-pilot")


if __name__ == "__main__":
    main()
