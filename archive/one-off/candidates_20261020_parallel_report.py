import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "one-off"))

from candidates_20261020_parallel import load_rows
from utils.batch_gate_checks import linked_query
from utils.monitor_price_store import atomic_json, now
from utils.monitor_price_writer_plan import BLANKET_CHECK_SQL
from utils.supabase_access import load_supabase_client

OUT = ROOT / "one-off/outputs/candidates_20261020"


def effective_status(row):
    error = str((row.get("discovery") or {}).get("error") or "")
    if row["status"] == "crawl_failed" and error.startswith("ConnectionError:") and "192.168.1.68" in error:
        return "pending_transport_recovery"
    return row["status"]


def main():
    rows = load_rows()
    seeded = [row for row in rows if row.get("master_verified_at")]
    unique_ids = sorted({row["business_id"] for row in seeded})
    ids = ",".join(str(value) for value in unique_ids)
    client = load_supabase_client(ROOT)
    actual = client.fetch_rows("master_business_info", "business_id,name,address,city,website,google_place_id", filters={"business_id": f"in.({ids})"}, limit=1000)
    assert len(actual) == len(unique_ids), "business_readback_incomplete"
    by_id = {row["business_id"]: row for row in actual}
    for row in seeded:
        assert by_id[row["business_id"]]["google_place_id"] == row["source"]["google place_id"], "business_identity_mismatch"
    baseline = {row["business_id"] for row in json.loads((OUT / "master_before.json").read_text())}
    new_businesses = set(unique_ids) - baseline
    sql = " union all ".join(f"select '{table}' as table_name,count(*) as rows from {table} where business_id in ({ids})" for table in ["clinic_services", "clinic_memberships", "clinic_promotions", "promo_offer_master"])
    sql += f" union all select 'promo_offer_items',count(*) from promo_offer_items i join promo_offer_master m on m.id=i.offer_id where m.business_id in ({ids})"
    counts = linked_query(sql, root=ROOT)
    blanket = linked_query(BLANKET_CHECK_SQL, root=ROOT)
    quality_sql = f"""
with scoped as (select * from promo_offer_master where is_active and business_id in ({ids}))
select 'active_offer_without_item' as check_name,count(*) as failures from scoped m
 where not exists(select 1 from promo_offer_items i where i.offer_id=m.id)
union all select 'cross_business_mapping',count(*) from scoped m
 join promo_offer_items i on i.offer_id=m.id join clinic_services s on s.service_id=i.service_id
 where s.business_id is distinct from m.business_id
union all select 'active_per_unit_missing_unit_price',count(*) from scoped m
 join promo_offer_items i on i.offer_id=m.id
 where m.price_model='per_unit' and (m.discount_price is null or i.unit_price is null)
union all select 'active_offer_on_invalid_source',count(*) from scoped m
 left join clinic_promotions p on p.promotion_id=m.promotion_id
 left join clinic_memberships c on c.plan_id=m.membership_plan_id
 where (m.promotion_id is not null and (p.is_active is distinct from true or p.business_id is distinct from m.business_id))
 or (m.membership_plan_id is not null and (c.plan_id is null or c.business_id is distinct from m.business_id))
 or (m.promotion_id is null and m.membership_plan_id is null)
"""
    quality = linked_query(quality_sql, root=ROOT)
    duplicates = linked_query(f"select google_place_id,count(*) as n from master_business_info where google_place_id in (select google_place_id from master_business_info where business_id in ({ids})) group by google_place_id having count(*)>1", root=ROOT)
    assert not duplicates, "duplicate_place_id"
    captures = [json.loads(path.read_text()) for path in sorted((OUT / "requests").glob("*/*.json"))]
    extractions = [json.loads(path.read_text()) for path in sorted((OUT / "extraction").glob("*/*.json"))]
    visibility = [json.loads(path.read_text()) for path in sorted((OUT / "visibility").glob("*/*.json"))]
    raw_counts = {}
    for table in ["firecrawl_search_raw", "firecrawl_scrape_raw"]:
        paths = [OUT / "raw" / f"{table}.jsonl", *(OUT / "batches").glob(f"batch_*/raw/{table}.jsonl")]
        raw_counts[table] = sum(sum(bool(line.strip()) for line in path.open()) for path in paths if path.exists())
    statuses = dict(Counter(effective_status(row) for row in rows))
    skipped = [row for row in rows if row["status"] == "skipped"]
    failures = [row for row in rows if effective_status(row) == "crawl_failed"]
    unfinished = [{"input_row": row["input_row"], "business_id": row.get("business_id"), "name": row["source"]["名称"], "status": effective_status(row), "reason": row.get("reason")} for row in rows if effective_status(row) not in {"skipped", "success", "no_promotion_page"}]
    progress = {"api_urls": sorted({item.get("api_url") or item.get("capture_method", "unknown") for item in captures}), "request_counts": dict(Counter(item.get("path") or item.get("capture_method", "unknown") for item in captures)), "businesses_with_saved_requests": len({path.parent.name for path in (OUT / "requests").glob("*/*.json")}), "extraction_pages": len(extractions), "source_name_contract_pages": sum(item.get("extraction_contracts", {}).get("offers") == "source_item_name_v1" for item in extractions), "luna_validated_pages": sum(bool(item.get("luna_source_path")) for item in extractions), "visibility_pages": len(visibility)}
    receipt = {"checked_at": now(), "businesses_verified": len(actual), "new_businesses": len(new_businesses), "table_counts": counts, "blanket_sql": BLANKET_CHECK_SQL, "blanket_rows": blanket, "quality_sql": quality_sql, "quality_checks": quality, "duplicate_place_id_rows": duplicates, "statuses": statuses, "capture_progress": progress, "raw_counts": raw_counts}
    atomic_json(OUT / "qa_readback.json", receipt)
    atomic_json(OUT / "failures.json", failures)
    atomic_json(OUT / "unfinished.json", unfinished)
    atomic_json(OUT / "parallel_summary.json", {**receipt, "rows": rows})
    report = ["# CostFinder 全名单并行批次 QA", "", f"数据库回读时间：{receipt['checked_at']}", "", "## 覆盖与执行", "", f"- 输入 {len(rows)} 家；官网为空 {len(skipped)} 家，保持跳过。", f"- 商家身份已回读 {len(actual)} 家；相对初始快照新增 {len(new_businesses)} 家。", "- 3 个 gpt-6-luna 子代理分别负责输入行 1–253、254–506、507–760。", "- 采集使用 http://192.168.1.68:3002；每批串行，最大采集并发为 3。", f"- 逐家状态：`{json.dumps(statuses, ensure_ascii=False)}`。", "- pending_transport_recovery 是执行环境此前的服务连接失败，保留原错误审计，未计为商家网站失败。", "", "## 原始记录、抽取与可见性", "", "```json", json.dumps(progress, ensure_ascii=False, indent=2), "```", "", "原始记录沿用仓库本地 JSONL 路由。Gemini 抽取调用已停止；Luna 子代理读取真实原始页面，按现有 schema 输出并通过共享验证入口后，进入后续可见性核验及写入计划。抽取文件不代表业务表写入完成。", "", "## 数据库回读", "", "| 表 | 本批商家当前行数 |", "| --- | ---: |", f"| master_business_info | {len(actual)} |"]
    report.extend(f"| {item['table_name']} | {item['rows']} |" for item in counts)
    report.extend(f"| {table}（本地原始记录） | {count} |" for table, count in raw_counts.items())
    report += ["", "## 本批 Active Offer 关系检查", "", "| 检查 | 异常数 |", "| --- | ---: |"]
    report.extend(f"| {item['check_name']} | {item['failures']} |" for item in quality)
    report += ["", "## Blanket Offer SQL", "", "```sql", BLANKET_CHECK_SQL.strip(), "```", "", f"结果：{len(blanket)} 行。", "", "```json", json.dumps(blanket, ensure_ascii=False, indent=2), "```", "", f"重复 place_id 检查：{len(duplicates)} 行。", "", "## 当前抓取失败", "", "| 输入行 | 名称 | 原因 |", "| ---: | --- | --- |"]
    report.extend(f"| {row['input_row']} | {row['source']['名称'].replace('|', '/')} | {str(row.get('reason', '')).replace('|', '/')} |" for row in failures)
    report += ["", "## 官网为空", "", "| 输入行 | 名称 | 城市 |", "| ---: | --- | --- |"]
    report.extend(f"| {row['input_row']} | {row['source']['名称'].replace('|', '/')} | {row['source']['城市']} |" for row in skipped)
    report += ["", "## 抽查抓取来源", "", "```json", json.dumps([{"path": item.get("path") or item.get("capture_method", "unknown"), "source_url": (item.get("request") or {}).get("url"), "api_url": item.get("api_url") or item.get("capture_method", "unknown"), "captured_at": item.get("captured_at")} for item in captures[:5]], ensure_ascii=False, indent=2), "```", "", "## 未完成项", "", "剩余商家采集、失败重试、完整抽取、当前可见性核验、业务表写入及最终验收仍需完成。服务档位价格冲突、共享促销重复出现、来源名称或价费引用未验证的记录保持复核。", "", "## 断点与脚本", "", "- batches/batch_N/status.json：各批次逐家状态。", "- batches/batch_N/luna_extraction/ 与 luna_validation.json：Luna 抽取及共享验证结果。", "- requests/、raw/、extraction/、visibility/：实际请求、原始记录、结构化结果及可见性证据。", "- unfinished.json、failures.json、qa_readback.json：未完成清单、抓取失败和数据库证据。", "- one-off/candidates_20261020_parallel.py：全量去重、商家写入回读及三个独立采集批次。", "- one-off/candidates_20261020_extract.py：批次抽取与 Luna 结果的 schema、来源及身份验证。", "- one-off/candidates_20261020_parallel_report.py：全量状态与数据库回读 QA。", ""]
    (OUT / "qa_report.md").write_text("\n".join(report))
    assert all(item["failures"] == 0 for item in quality), "offer_relationship_quality_failed"
    print(json.dumps({"masters_verified": len(actual), "new_businesses": len(new_businesses), "statuses": statuses, "table_counts": counts, "raw_counts": raw_counts, "blanket_rows": len(blanket)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
