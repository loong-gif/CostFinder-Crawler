"""原始爬取结果抽取为 clinic_promotions / clinic_memberships。数据库读写经 Supabase MCP：

  read-sql [--decisions F --business-id N --limit N]
                      按决策文件涉及的商家生成只读快照查询 read.sql，经 MCP 执行后把 snapshot 列的值存为 snapshot.json
  plan --snapshot F [--decisions F --business-id N --limit N --raw-dir D --visibility-manifest M]
                      根据快照、原始爬取结果与可见性清单生成 plan.json（调用 LLM）；筛选参数与 read-sql 保持一致
  apply-sql --plan F --visibility-manifest M
                      核对可见性清单哈希后把计划渲染为 apply.sql（单个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from utils.discovery_batch import DECISIONS
from utils.offer_extraction_llm import build_client_from_env
from utils.page_extraction import build_plan, render_apply
from utils.page_extraction_snapshot import (
    database_state,
    latest_scrapes,
    load_decision_pages,
    snapshot_tables_for_pages,
)
from utils.ego_browser import fetch_page_title
from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.raw_file_store import RawFileStore

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "extract_pages"
SCHEMA_DIR = PROJECT_ROOT / "schema"


def _load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text())
    return payload.get("results", payload)


def _selected_pages(args: argparse.Namespace) -> list[dict]:
    return load_decision_pages(json.loads(args.decisions.read_text()),
                               business_ids=set(args.business_id) if args.business_id else None, limit=args.limit)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)

    def add_selection(p: argparse.ArgumentParser) -> None:
        p.add_argument("--decisions", type=Path, default=DECISIONS)
        p.add_argument("--limit", type=int, help="按 URL 排序后只处理前 N 个页面")
        p.add_argument("--business-id", type=int, action="append", help="只处理指定商家，可重复")

    add_selection(sub.add_parser("read-sql"))
    plan_parser = sub.add_parser("plan")
    add_selection(plan_parser)
    plan_parser.add_argument("--snapshot", type=Path, required=True)
    plan_parser.add_argument("--raw-dir", type=Path, help="原始爬取结果目录，默认 COSTFINDER_RAW_DIR")
    plan_parser.add_argument("--visibility-manifest", type=Path, help="渲染可见性清单；apply-sql 必须使用同一份")
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--visibility-manifest", type=Path, required=True,
                              help="生成计划时使用的同一份渲染可见性清单")
    args = parser.parse_args()

    if args.step == "read-sql":
        pages = _selected_pages(args)
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(write_text(run_dir / "read.sql", snapshot_query(snapshot_tables_for_pages(pages))))
        return 0

    if args.step == "apply-sql":
        plan = json.loads(args.plan.read_text())
        print(write_text(args.plan.parent / "apply.sql", render_apply(plan, _load_manifest(args.visibility_manifest))))
        return 0

    load_dotenv(PROJECT_ROOT / ".env")
    llm = build_client_from_env()
    if llm is None:
        raise RuntimeError("LLM_API_KEY / LLM_MODEL are not configured")
    pages = _selected_pages(args)
    snapshot = load_snapshot(args.snapshot, snapshot_tables_for_pages(pages))
    state = database_state(snapshot, sorted({p["business_id"] for p in pages}))
    store = RawFileStore(args.raw_dir) if args.raw_dir else RawFileStore()
    scrapes = latest_scrapes(store.fetch_rows("firecrawl_scrape_raw", "*", filters={"success": "eq.true"}))
    plan = build_plan(
        pages, scrapes=scrapes, state=state, llm=llm, fetch_title=fetch_page_title,
        cache_dir=OUTPUT_DIR / "llm_cache",
        manifest=_load_manifest(args.visibility_manifest) if args.visibility_manifest else None,
        promotion_schema=json.loads((SCHEMA_DIR / "promotion_extraction_schema.json").read_text()),
        membership_schema=json.loads((SCHEMA_DIR / "membership_extraction_schema.json").read_text()))
    out = write_text(args.snapshot.parent / "plan.json", json.dumps(plan, ensure_ascii=False, indent=2, default=str))
    print(f"{out}\n{json.dumps(plan['summary'])}")
    return 0
