"""合并重复促销与 hub 子页面。数据库读写经 Supabase MCP：

  read-sql            生成只读快照查询 read.sql，经 MCP 执行后把 snapshot 列的值存为 snapshot.json
  plan --snapshot F   根据快照生成 plan.json（抓取 hub 页面标题）
  apply-sql --plan F  把计划渲染为 apply.sql（单个 DO 块），经 MCP 执行
  verify --plan F --snapshot S
                      执行后重新 read-sql + read-snapshot，核对库中结果与计划逐项一致
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from utils.ego_browser import fetch_page_title
from utils.pipeline_db import load_snapshot, snapshot_query, write_text
from utils.pipeline_run import PROJECT_ROOT
from utils.promotion_merge import SNAPSHOT_TABLES, build_plan, dump_plan, render_apply, verify_applied

OUTPUT_DIR = PROJECT_ROOT / "output" / "pipeline" / "merge_promotions"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="step", required=True)
    sub.add_parser("read-sql")
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--snapshot", type=Path, required=True)
    apply_parser = sub.add_parser("apply-sql")
    apply_parser.add_argument("--plan", type=Path, required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--plan", type=Path, required=True)
    verify_parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()

    if args.step == "verify":
        problems = verify_applied(json.loads(args.plan.read_text()), load_snapshot(args.snapshot, SNAPSHOT_TABLES))
        print("\n".join(problems) or "verified: database matches plan")
        return 1 if problems else 0

    if args.step == "read-sql":
        run_dir = OUTPUT_DIR / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        print(write_text(run_dir / "read.sql", snapshot_query(SNAPSHOT_TABLES)))
        return 0
    if args.step == "plan":
        plan = build_plan(load_snapshot(args.snapshot, SNAPSHOT_TABLES), fetch_page_title)
        out = write_text(args.snapshot.parent / "plan.json", dump_plan(plan))
        ready = sum(g["status"] == "ready" for g in plan["groups"])
        print(f"{out}\ngroups={len(plan['groups'])} ready={ready} blocked={len(plan['groups']) - ready}")
        return 0
    plan = json.loads(args.plan.read_text())
    print(write_text(args.plan.parent / "apply.sql", render_apply(plan)))
    return 0
