"""第 5 段：从 promotion_content / benefits 抽取 promo_offer_master + promo_offer_items。数据库读写经 Supabase MCP：

  read-sql [--promotion-id N] [--plan-id N] [--limit N]  生成只读快照查询 read.sql，经 MCP 执行后把 snapshot 列的值存为 snapshot.json
  plan --snapshot F                                      根据快照调用 LLM 生成 plan.json
  apply-sql --plan F                                     把计划渲染为 apply.sql（单个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from utils.offer_extraction_llm import build_client_from_env
from utils.offer_schema import offer_schema, service_name_enum
from utils.offer_stage import build_plan, dump_plan, render_apply, snapshot_tables
from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "extract_offers"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    read_parser = sub.add_parser("read-sql")
    read_parser.add_argument("--promotion-id", type=int, action="append", help="只处理指定促销（可重复）；指定后不再按 is_offer_extracted 选取")
    read_parser.add_argument("--plan-id", type=int, action="append", help="只处理指定会员计划（可重复）")
    read_parser.add_argument("--limit", type=int, help="最多处理的源数量")
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--snapshot", type=Path, required=True)
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.step == "read-sql":
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        tables = snapshot_tables(args.promotion_id, args.plan_id, args.limit)
        print(write_text(run_dir / "read.sql", snapshot_query(tables)))
        return 0
    if args.step == "plan":
        load_dotenv(PROJECT_ROOT / ".env")
        client = build_client_from_env()
        if client is None:
            raise RuntimeError("LLM client not configured (LLM_API_KEY / LLM_MODEL)")
        snapshot = load_snapshot(args.snapshot, snapshot_tables())
        schema = offer_schema(service_name_enum(snapshot["service_enum"]))
        plan = build_plan(snapshot, client, schema, OUTPUT_DIR / "llm_cache")
        out = write_text(args.snapshot.parent / "plan.json", dump_plan(plan))
        ready = sum(s["status"] == "ready" for s in plan["sources"])
        print(f"{out}\nsources={len(plan['sources'])} ready={ready} blocked={len(plan['sources']) - ready}")
        return 0
    plan = json.loads(args.plan.read_text())
    print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
