"""从 offer 识别 service，回填 clinic_services 与 promo_offer_items.service_id。数据库读写经 Supabase MCP：

  read-sql                  生成只读快照查询 read.sql，经 MCP 执行后把 snapshot 列的值存为 snapshot.json
  plan --snapshot F         根据快照生成 plan.json（需要时调用 LLM）；--skip-llm 不调用 LLM
  apply-sql --plan F        把计划渲染为 apply.sql（单个 DO 块），经 MCP 执行
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from utils.offer_extraction_llm import build_client_from_env
from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.service_linking import SNAPSHOT_TABLES, build_plan, dump_plan, render_apply

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "link_services"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("read-sql")
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--snapshot", type=Path, required=True)
    plan_parser.add_argument("--skip-llm", action="store_true",
                             help="不调用 LLM；确定性规则无法识别的 item 仍报错终止，需先处理")
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "read-sql":
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(write_text(run_dir / "read.sql", snapshot_query(SNAPSHOT_TABLES)))
        return 0
    if args.step == "plan":
        load_dotenv(PROJECT_ROOT / ".env")
        client = None if args.skip_llm else build_client_from_env()
        if client is None and not args.skip_llm:
            raise RuntimeError("LLM client is not configured (LLM_API_KEY / LLM_MODEL / LLM_API_URL)")
        plan = build_plan(load_snapshot(args.snapshot, SNAPSHOT_TABLES), client)
        out = write_text(args.snapshot.parent / "plan.json", dump_plan(plan))
        print(f"{out}\n{json.dumps(plan['counts'])} services_to_create={len(plan['services_to_create'])} "
              f"promotions_to_mark={len(plan['promotions_to_mark'])} memberships_to_mark={len(plan['memberships_to_mark'])}")
        return 0
    plan = json.loads(args.plan.read_text())
    print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
    return 0
