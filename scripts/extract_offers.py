"""第 5 段：从 promotion_content / benefits 抽取 promo_offer_master + promo_offer_items：

  plan [--limit N] [--snapshot F]       读取只读快照（未给出时自动执行 read.sql 生成 snapshot.json），调用 LLM 生成 plan.json；
                                         来源自动选取：尚无 offer 的促销与会员、未激活 offer 已在写入后完成服务关联的来源
  apply-sql --plan F [--batch-size N]    把计划渲染为 apply.sql（单个 DO 块），经 MCP 执行；--batch-size 拆成多个独立 DO 块
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from utils import pipeline_db
from utils.offer_extraction_llm import build_client_from_env
from utils.offer_schema import offer_schema, service_name_enum
from utils.offer_stage import build_plan, dump_plan, snapshot_tables
from utils.offer_stage_sql import render_apply
from utils.pipeline_db import load_snapshot, write_text
from utils.pipeline_run import PROJECT_ROOT

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "extract_offers"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--limit", type=int, help="最多处理的源数量")
    plan_parser.add_argument("--snapshot", type=Path)
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    apply_parser.add_argument("--batch-size", type=int, help="按每批 N 个来源拆成 apply_01.sql、apply_02.sql ...，每批是独立的 DO 块")
    args = parser.parse_args(argv)

    if args.step == "plan":
        load_dotenv(PROJECT_ROOT / ".env")
        client = build_client_from_env()
        if client is None:
            raise RuntimeError("LLM client not configured (LLM_API_KEY / LLM_MODEL)")
        snapshot_path = args.snapshot or pipeline_db.fresh_snapshot(
            pipeline_db.run_dir(OUTPUT_DIR), snapshot_tables(limit=args.limit))
        snapshot = load_snapshot(snapshot_path, snapshot_tables())
        schema = offer_schema(service_name_enum(snapshot["service_enum"]))
        plan = build_plan(snapshot, client, schema, OUTPUT_DIR / "llm_cache")
        out = write_text(snapshot_path.parent / "plan.json", dump_plan(plan))
        writable = sum(bool(s["offers"]) for s in plan["sources"])
        print(f"{out}\nsources={len(plan['sources'])} with_offer_actions={writable}")
        return 0
    plan = json.loads(args.plan.read_text())
    if args.batch_size is None:
        print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
        return 0
    writable = [s for s in plan["sources"] if s["offers"]]
    for start in range(0, len(writable), args.batch_size):
        batch = {**plan, "sources": writable[start:start + args.batch_size]}
        name = f"apply_{start // args.batch_size + 1:02d}.sql"
        print(write_text(args.plan.parent / name, render_apply(batch)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
