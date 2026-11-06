import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.batch_gate_checks import linked_query
from utils.monitor_price_store import atomic_json, now
from utils.monitor_price_writer_plan import BLANKET_CHECK_SQL
from utils.supabase_access import load_supabase_client

OUT = ROOT / "one-off/outputs/candidates_20261028_208"


def capture_progress(rows, selected):
    selected = set(selected)
    requests = [json.loads(path.read_text()) for path in sorted((OUT / "requests").glob("*/*.json")) if int(path.parent.name) in selected]
    extractions = [json.loads(path.read_text()) for path in sorted((OUT / "extraction").glob("*/*.json")) if int(path.parent.name) in selected]
    kinds = {"promotions", "memberships", "services", "offers"}
    assert all(record["input_row"] in selected for record in extractions)
    return {
        "api_urls": sorted({record["api_url"] for record in requests}),
        "requests": dict(Counter(record["path"] for record in requests)),
        "discovery_completed": sum(bool(row.get("discovery_completed_at")) for row in rows if row["input_row"] in selected),
        "scraped_pages": sum(record["path"] == "/v1/scrape" and bool((record["response"].get("data") or {}).get("markdown", "").strip()) for record in requests),
        "extraction_started": len(extractions),
        "extraction_completed": sum(kinds <= set(record["extractions"]) for record in extractions),
        "extraction_kinds": dict(Counter(kind for record in extractions for kind in record["extractions"])),
        "samples": [{"input_row": record["input_row"], "source_url": record["source_url"], "captured_at": record["captured_at"], "kinds": sorted(record["extractions"])} for record in extractions[:5]],
    }


def main():
    rows = json.loads((OUT / "business_status.json").read_text())
    selected = json.loads((OUT / "pilot_rows.json").read_text())
    progress = capture_progress(rows, selected)
    seeded = [r for r in rows if r.get("master_verified_at")]
    ids = ",".join(str(int(r["business_id"])) for r in seeded)
    assert len(seeded) == 20 and len(set(ids.split(","))) == 20
    client = load_supabase_client(ROOT)
    actual = client.fetch_rows("master_business_info", "business_id,name,address,city,website,google_place_id", filters={"business_id": f"in.({ids})"})
    assert len(actual) == 20
    by_id = {r["business_id"]: r for r in actual}
    for row in seeded:
        record = by_id[row["business_id"]]
        assert all(record[column] == row["source"][source] for column, source in [("name", "名称"), ("address", "地址"), ("city", "城市"), ("website", "官网"), ("google_place_id", "google place_id")])
        if row["status"] == "pending":
            row.update(status="pending_crawl", reason="商家已写入并回读，等待试跑抓取")
    atomic_json(OUT / "business_status.json", rows)
    count_sql = " union all ".join(f"select '{table}' as table_name,count(*) as rows from {table} where business_id in ({ids})" for table in ["clinic_services", "clinic_memberships", "clinic_promotions", "promo_offer_master"])
    count_sql += f" union all select 'promo_offer_items',count(*) from promo_offer_items i join promo_offer_master m on m.id=i.offer_id where m.business_id in ({ids})"
    counts = linked_query(count_sql, root=ROOT)
    blanket = linked_query(BLANKET_CHECK_SQL, root=ROOT)
    duplicates = linked_query(f"select google_place_id,count(*) as n from master_business_info where google_place_id in (select google_place_id from master_business_info where business_id in ({ids})) group by google_place_id having count(*)>1", root=ROOT)
    assert not duplicates, "pilot_duplicate_place_id"
    receipt = {"checked_at": now(), "pilot_businesses": actual, "table_counts": counts, "blanket_sql": BLANKET_CHECK_SQL, "blanket_rows": blanket, "duplicate_place_id_rows": duplicates, "capture_progress": progress}
    atomic_json(OUT / "qa_readback.json", receipt)
    summary = Counter(r["status"] for r in rows)
    skipped = [r for r in rows if r["status"] == "skipped"]
    failures = [r for r in rows if r["status"] == "crawl_failed"]
    pending = [r for r in rows if r["status"] not in {"skipped", "success", "no_promotion_page"}]
    atomic_json(OUT / "failures.json", failures)
    atomic_json(OUT / "unfinished.json", [{"input_row": r["input_row"], "name": r["source"]["名称"], "business_id": r.get("business_id"), "status": r["status"], "reason": r.get("reason", "试跑验收前尚未开始处理")} for r in pending])
    raw_counts = {}
    for table in ["firecrawl_search_raw", "firecrawl_scrape_raw"]:
        file = OUT / "raw" / f"{table}.jsonl"
        raw_counts[table] = sum(1 for line in file.open() if line.strip()) if file.exists() else 0
    report = ["# 新候选商家批次 QA", "", f"检查时间：{receipt['checked_at']}", "", "当前阶段：20 家试跑抓取与结构化抽取；业务写入及试跑验收尚未完成，全量尚未开始。", "", "## 覆盖情况", "", "- 输入：760 家；品类与 place_id 唯一性检查通过。", f"- 官网为空：{len(skipped)} 家，已跳过。", "- 有官网：751 家。", "- 试跑：20 家，已新增并独立回读确认 20 家商家。", f"- 已完成页面发现：{progress['discovery_completed']} 家。", f"- 已保存正文：{progress['scraped_pages']} 页；已开始抽取：{progress['extraction_started']} 页；完成四类结构化抽取：{progress['extraction_completed']} 页（dry run）。", "- 其余 731 家有官网商家尚未写入。", f"- 逐家状态：`{json.dumps(dict(summary), ensure_ascii=False)}`。", "", "## 本批次新增记录", "", "| 表或原始记录 | 新增行数 |", "| --- | ---: |", "| master_business_info | 20 |"]
    report.extend(f"| {r['table_name']} | {r['rows']} |" for r in counts)
    report.extend(f"| {table}（批次本地 JSONL） | {count} |" for table, count in raw_counts.items())
    report += ["", "20 家均为本批次新 business_id，相关业务表初始为零；表中数量来自当前数据库回读。raw 表沿用现有 SupabaseRestClient 的本地 JSONL 路由，未创建数据库表。", "", "## 去重与品牌门店", "", "已对 419 家现有商家进行分页读取，并通过 SQL 读取其已存在的 phone 字段，核对 place_id、官网域名与门牌号、电话、名称与地址。751 家有官网候选均未匹配现有商家。写入字段限定为 snapshot 中的字段，未改动 schema。", "", "当前 CSV 的名称和备注未出现 Simplicity Laser、NakedMD 或 West Dermatology。已读取现有品牌门店记录；本次没有从输入以外增加门店，也没有复制共享品牌优惠。", "", "## 抓取与抽取进度", "", "本批次已按用户要求切换自部署 Firecrawl http://192.168.1.68:3002，采用串行请求。", f"已保存请求的实际 API 地址：{json.dumps(progress['api_urls'], ensure_ascii=False)}。", f"实际请求记录：{json.dumps(progress['requests'], ensure_ascii=False)}。", f"已保存的抽取类别结果：{json.dumps(progress['extraction_kinds'], ensure_ascii=False)}。", "", "尚需完成：剩余试跑页面发现与抓取、全部对应页面的结构化抽取、当前可见性核验、业务表写入及验收；试跑通过后再处理其余 731 家。抽取文件处于 dry run 阶段。", "", f"抓取失败记录：{len(failures)} 家。逐家状态与原因见 business_status.json、failures.json、unfinished.json。尚未完成发现的商家不能据此判断无促销页。", "", "## Blanket Offer SQL", "", "```sql", BLANKET_CHECK_SQL.strip(), "```", "", f"结果：{len(blanket)} 行。", "", "```json", json.dumps(blanket, ensure_ascii=False, indent=2), "```", "", "该结果来自当前数据库检查；结构化抽取文件不代表 Offer 或 Item 已完成写入。", "", "## 试跑商家回读样例", "", "| 输入行 | business_id | 名称 | 城市 |", "| ---: | ---: | --- | --- |"]
    report.extend(f"| {r['input_row']} | {r['business_id']} | {r['source']['名称'].replace('|', '/')} | {r['source']['城市']} |" for r in seeded)
    report += ["", "逐家回读的名称、地址、城市、官网、place_id 均与输入一致；重复 place_id SQL 返回 0 行。", "", "### 抽取断点样例（dry run）", "", "```json", json.dumps(progress['samples'], ensure_ascii=False, indent=2), "```", "", "样例仅证明对应页面已保存抽取结果；业务表写入与验收仍需完成。", "", "## 官网为空的跳过清单", "", "| 输入行 | 名称 | 城市 | 地址 | 原因 |", "| ---: | --- | --- | --- | --- |"]
    report.extend(f"| {r['input_row']} | {r['source']['名称'].replace('|', '/')} | {r['source']['城市']} | {r['source']['地址'].replace('|', '/')} | 官网为空 |" for r in skipped)
    report += ["", "## 断点文件", "", "- business_status.json：760 家完整输入、状态、原因和商家写入／回读证据。", "- pilot_rows.json：固定的 20 家试跑输入行号。", "- qa_readback.json：当前数据库回读、SQL 检查结果及本地抓取与抽取统计。", "- requests/：实际 Firecrawl 请求、响应、API 地址和抓取时间。", "- extraction/：按抓取页面保存的结构化抽取断点，当前为 dry run。", "- failures.json、unfinished.json：抓取失败与未完成记录。", "", "从仓库目录运行 `.venv/bin/python one-off/candidates_20261028_208_run.py crawl-pilot` 可续跑发现抓取；运行 `.venv/bin/python one-off/candidates_20261028_208_extract.py` 可续跑结构化抽取。各阶段应避免重复启动同一任务。`seed-pilot` 可幂等核对商家。已经写入后，preflight 会拒绝重建原始断点。", ""]
    report += ["## 本次文件变更", "", "- one-off/candidates_20261028_208_run.py：自部署 Firecrawl 地址切换及请求保存。", "- one-off/candidates_20261028_208_extract.py：按真实抓取页面执行可续跑的四类结构化抽取。", "- one-off/candidates_20261028_208_report.py：数据库回读及依据当前断点生成 QA 报告。", "- one-off/outputs/candidates_20261028_208/：本批次请求、原始记录、抽取断点、状态、检查证据及 QA 报告。", "", "已验证的商家与业务表数量以本次数据库回读为准；实际请求与抽取数量来自对应文件。尚需完成可见性核验、业务表写入及试跑验收。", ""]
    report += ["- one-off/candidates_20261028_208_visibility.mjs：在同一 Ego 任务空间保存页面文本布局、祖先样式及逐条声明的可见性证据。", ""]
    (OUT / "qa_report.md").write_text("\n".join(report))
    print(json.dumps({"masters_verified": len(actual), "blanket_rows": len(blanket), "table_counts": counts, "statuses": dict(summary), "raw_counts": raw_counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
