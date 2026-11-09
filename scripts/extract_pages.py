"""原始爬取结果抽取为 clinic_promotions / clinic_memberships：

  plan [--decisions F --business-id N --limit N --raw-dir D --snapshot F]
                      读取决策文件涉及商家的只读快照（未给出时自动执行 read.sql 生成 snapshot.json），
                      生成计划后自动完成可见性核验（分片、ego-lite 渲染、可见性清单），再按清单生成最终 plan.json；
                      中间文件与 visibility_manifest.json 写在同一运行目录
  apply-sql --plan F  核对同目录 visibility_manifest.json 的哈希后把计划渲染为 apply.sql（单个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import functools
import json
from pathlib import Path

from dotenv import load_dotenv

from utils import pipeline_db
from utils.ego_browser import fetch_page_title
from utils.extract_pages_run import plan_with_visibility
from utils.offer_extraction_llm import build_client_from_env
from utils.page_extraction import build_plan, render_apply
from utils.page_extraction_snapshot import (
    database_state,
    latest_scrapes,
    load_decision_pages,
    snapshot_tables_for_pages,
)
from utils.pipeline_db import load_snapshot, write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.raw_file_store import RawFileStore

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "extract_pages"
SCHEMA_DIR = PROJECT_ROOT / "schema"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--decisions", type=Path, default=PROJECT_ROOT / "state/discovery/monitor_decisions_final.json")
    plan_parser.add_argument("--limit", type=int, help="按 URL 排序后只处理前 N 个页面")
    plan_parser.add_argument("--business-id", type=int, action="append", help="只处理指定商家，可重复")
    plan_parser.add_argument("--snapshot", type=Path)
    plan_parser.add_argument("--raw-dir", type=Path, help="原始爬取结果目录，默认 COSTFINDER_RAW_DIR")
    sub.add_parser("apply-sql").add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "apply-sql":
        plan = json.loads(args.plan.read_text())
        manifest = json.loads((args.plan.parent / "visibility_manifest.json").read_text())
        print(write_text(args.plan.parent / "apply.sql", render_apply(plan, manifest)))
        return 0

    load_dotenv(PROJECT_ROOT / ".env")
    llm = build_client_from_env()
    if llm is None:
        raise RuntimeError("LLM_API_KEY / LLM_MODEL are not configured")
    pages = load_decision_pages(json.loads(args.decisions.read_text()),
                                business_ids=set(args.business_id) if args.business_id else None, limit=args.limit)
    tables = snapshot_tables_for_pages(pages)
    snapshot_path = args.snapshot or pipeline_db.fresh_snapshot(pipeline_db.run_dir(OUTPUT_DIR), tables)
    state = database_state(load_snapshot(snapshot_path, tables), sorted({p["business_id"] for p in pages}))
    store = RawFileStore(args.raw_dir) if args.raw_dir else RawFileStore()
    scrapes = latest_scrapes(store.fetch_rows("firecrawl_scrape_raw", "*", filters={"success": "eq.true"}))
    schemas = {name: json.loads((SCHEMA_DIR / f"{name}_extraction_schema.json").read_text())
               for name in ("promotion", "membership")}

    # 两次计划共用同一次标题抓取
    titles = functools.cache(fetch_page_title)

    def plan_once(manifest):
        return build_plan(pages, scrapes=scrapes, state=state, llm=llm, fetch_title=titles,
                          cache_dir=OUTPUT_DIR / "llm_cache", manifest=manifest,
                          promotion_schema=schemas["promotion"], membership_schema=schemas["membership"])

    plan, _ = plan_with_visibility(snapshot_path.parent, plan_once)
    out = write_text(snapshot_path.parent / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    print(f"{out}\n{json.dumps(plan['summary'])}")
    return 0
